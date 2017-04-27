import os
import datetime
from functools import partial
import base64
import binascii

# Find the best implementation available on this platform
try:
    from cStringIO import StringIO
except:
    from StringIO import StringIO

from sqlalchemy import create_engine, ForeignKey
from sqlalchemy.orm import scoped_session, sessionmaker, relationship, backref
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Binary
from sqlalchemy.orm.exc import MultipleResultsFound, NoResultFound

import scrypt
import pyotp

import qrcode
# Using svg because it doesn't require additional dependencies
import qrcode.image.svg

import config
import store

LOGIN_HARDENING = True
# Unfortunately, the login hardening measures mess with the tests in
# non-deterministic ways.  TODO rewrite the tests so we can more
# precisely control which code paths are exercised.
if os.environ.get('SECUREDROP_ENV') == 'test':
    LOGIN_HARDENING = False

# http://flask.pocoo.org/docs/patterns/sqlalchemy/

if config.DATABASE_ENGINE == "sqlite":
    engine = create_engine(
        config.DATABASE_ENGINE + ":///" +
        config.DATABASE_FILE
    )
else:  # pragma: no cover
    engine = create_engine(
        config.DATABASE_ENGINE + '://' +
        config.DATABASE_USERNAME + ':' +
        config.DATABASE_PASSWORD + '@' +
        config.DATABASE_HOST + '/' +
        config.DATABASE_NAME, echo=False
    )

db_session = scoped_session(sessionmaker(autocommit=False,
                                         autoflush=False,
                                         bind=engine))
Base = declarative_base()
Base.query = db_session.query_property()


def get_one_or_else(query, logger, failure_method):
    try:
        return query.one()
    except MultipleResultsFound as e:
        logger.error(
            "Found multiple while executing %s when one was expected: %s" %
            (query, e, ))
        failure_method(500)
    except NoResultFound as e:
        logger.error("Found none when one was expected: %s" % (e,))
        failure_method(404)


class Source(Base):
    __tablename__ = 'sources'
    id = Column(Integer, primary_key=True)
    filesystem_id = Column(String(96), unique=True)
    journalist_designation = Column(String(255), nullable=False)
    flagged = Column(Boolean, default=False)
    last_updated = Column(DateTime, default=datetime.datetime.utcnow)
    star = relationship("SourceStar", uselist=False, backref="source")

    # sources are "pending" and don't get displayed to journalists until they
    # submit something
    pending = Column(Boolean, default=True)

    # keep track of how many interactions have happened, for filenames
    interaction_count = Column(Integer, default=0, nullable=False)

    # Don't create or bother checking excessively long codenames to prevent DoS
    MAX_CODENAME_LEN = 128

    def __init__(self, filesystem_id=None, journalist_designation=None):
        self.filesystem_id = filesystem_id
        self.journalist_designation = journalist_designation

    def __repr__(self):
        return '<Source %r>' % (self.journalist_designation)

    @property
    def journalist_filename(self):
        valid_chars = 'abcdefghijklmnopqrstuvwxyz1234567890-_'
        return ''.join([c for c in self.journalist_designation.lower().replace(
            ' ', '_') if c in valid_chars])

    def documents_messages_count(self):
        try:
            return self.docs_msgs_count
        except AttributeError:
            self.docs_msgs_count = {'messages': 0, 'documents': 0}
            for submission in self.submissions:
                if submission.filename.endswith('msg.gpg'):
                    self.docs_msgs_count['messages'] += 1
                elif submission.filename.endswith('doc.gz.gpg') or submission.filename.endswith('doc.zip.gpg'):
                    self.docs_msgs_count['documents'] += 1
            return self.docs_msgs_count

    @property
    def collection(self):
        """Return the list of submissions and replies for this source, sorted
        in ascending order by the filename/interaction count."""
        collection = []
        collection.extend(self.submissions)
        collection.extend(self.replies)
        collection.sort(key=lambda x: int(x.filename.split('-')[0]))
        return collection


class Submission(Base):
    __tablename__ = 'submissions'
    id = Column(Integer, primary_key=True)
    source_id = Column(Integer, ForeignKey('sources.id'))
    source = relationship(
        "Source",
        backref=backref("submissions", order_by=id, cascade="delete")
        )

    filename = Column(String(255), nullable=False)
    size = Column(Integer, nullable=False)
    downloaded = Column(Boolean, default=False)

    def __init__(self, source, filename):
        self.source_id = source.id
        self.filename = filename
        self.size = os.stat(store.path(source.filesystem_id, filename)).st_size

    def __repr__(self):
        return '<Submission %r>' % (self.filename)


class Reply(Base):
    __tablename__ = "replies"
    id = Column(Integer, primary_key=True)

    journalist_id = Column(Integer, ForeignKey('journalists.id'))
    journalist = relationship(
        "Journalist",
        backref=backref(
            'replies',
            order_by=id))

    source_id = Column(Integer, ForeignKey('sources.id'))
    source = relationship(
        "Source",
        backref=backref("replies", order_by=id, cascade="delete")
        )

    filename = Column(String(255), nullable=False)
    size = Column(Integer, nullable=False)

    def __init__(self, journalist, source, filename):
        self.journalist_id = journalist.id
        self.source_id = source.id
        self.filename = filename
        self.size = os.stat(store.path(source.filesystem_id, filename)).st_size

    def __repr__(self):
        return '<Reply %r>' % (self.filename)


class SourceStar(Base):
    __tablename__ = 'source_stars'
    id = Column("id", Integer, primary_key=True)
    source_id = Column("source_id", Integer, ForeignKey('sources.id'))
    starred = Column("starred", Boolean, default=True)

    def __eq__(self, other):
        if isinstance(other, SourceStar):
            return self.source_id == other.source_id and self.id == other.id and self.starred == other.starred
        return NotImplemented

    def __init__(self, source, starred=True):
        self.source_id = source.id
        self.starred = starred

class LoginException(Exception):
    """Generic exception for errors during login."""

class InvalidUsernameException(LoginException):
    """Raised when a user logs in with an invalid username."""

class LoginThrottledException(LoginException):
    """Raised when a user attempts to log in too many times in a given
    time period."""

class WrongPasswordException(LoginException):
    """Raised when a user logs in with an incorrect password."""

class BadTokenException(LoginException):
    """Raised when a user logs in with an invalid TOTP token."""

class TokenReuseException(LoginException):
    """Raised when a user attempts to re-use a token, or to use a token
    that preceds or proceeds a token that has been used."""

class InvalidPasswordLength(Exception):
    """Raised when attempting to create a Journalist or log in with an
    invalid password length.
    """
    def __init__(self, password):
        self.pw_len = len(password)

    def __str__(self):
        if self.pw_len > Journalist.MAX_PASSWORD_LEN:
            return "Password too long (len={})".format(self.pw_len)
        if self.pw_len < Journalist.MIN_PASSWORD_LEN:
            return "Password needs to be at least {} characters".format(
                Journalist.MIN_PASSWORD_LEN
            )


class Journalist(Base):
    __tablename__ = "journalists"
    id = Column(Integer, primary_key=True)
    username = Column(String(255), nullable=False, unique=True)
    pw_salt = Column(Binary(32))
    pw_hash = Column(Binary(256))
    is_admin = Column(Boolean)

    otp_secret = Column(String(16), default=pyotp.random_base32)
    is_totp = Column(Boolean, default=True)
    hotp_counter = Column(Integer, default=0)
    last_token = Column(String(6)) # deprecated

    created_on = Column(DateTime, default=datetime.datetime.utcnow)
    last_access = Column(DateTime)
    login_attempts = relationship(
        "JournalistLoginAttempt",
        backref="journalist")

    def __init__(self, username, password, is_admin=False, otp_secret=None):
        self.username = username
        self.set_password(password)
        self.is_admin = is_admin
        if otp_secret:
            self.set_hotp_secret(otp_secret)

    def __repr__(self):
        return "<Journalist {0}{1}>".format(
            self.username,
            " [admin]" if self.is_admin else "")

    def _gen_salt(self, salt_bytes=32):
        return os.urandom(salt_bytes)

    _SCRYPT_PARAMS = dict(N=2**14, r=8, p=1)

    def _scrypt_hash(self, password, salt, params=None):
        if not params:
            params = self._SCRYPT_PARAMS
        return scrypt.hash(str(password), salt, **params)

    MAX_PASSWORD_LEN = 128
    MIN_PASSWORD_LEN = 12

    def set_password(self, password):
        # Don't do anything if user's password hasn't changed.
        if self.pw_hash and self.valid_password(password):
            return
        # Enforce a reasonable maximum length for passwords to avoid DoS
        if len(password) > self.MAX_PASSWORD_LEN:
            raise InvalidPasswordLength(password)
        # Enforce a reasonable minimum length for new passwords
        if len(password) < self.MIN_PASSWORD_LEN:
            raise InvalidPasswordLength(password)
        self.pw_salt = self._gen_salt()
        self.pw_hash = self._scrypt_hash(password, self.pw_salt)

    def valid_password(self, password):
        # Avoid hashing passwords that are over the maximum length
        if len(password) > self.MAX_PASSWORD_LEN:
            raise InvalidPasswordLength(password)
        # No check on minimum password length here because some passwords
        # may have been set prior to setting the minimum password length.
        return pyotp.utils.compare_digest(
            self._scrypt_hash(password, self.pw_salt),
            self.pw_hash)

    def regenerate_totp_shared_secret(self):
        self.otp_secret = pyotp.random_base32()

    def set_hotp_secret(self, otp_secret):
        self.is_totp = False
        self.otp_secret = base64.b32encode(
            binascii.unhexlify(
                otp_secret.replace(
                    " ",
                    "")))
        self.hotp_counter = 0

    @property
    def totp(self):
        return pyotp.TOTP(self.otp_secret)

    @property
    def hotp(self):
        return pyotp.HOTP(self.otp_secret)

    @property
    def shared_secret_qrcode(self):
        uri = self.totp.provisioning_uri(
            self.username,
            issuer_name="SecureDrop")

        qr = qrcode.QRCode(
            box_size=15,
            image_factory=qrcode.image.svg.SvgPathImage
        )
        qr.add_data(uri)
        img = qr.make_image()

        svg_out = StringIO()
        img.save(svg_out)
        return svg_out.getvalue()

    @property
    def formatted_otp_secret(self):
        """The OTP secret is easier to read and manually enter if it is all
        lowercase and split into four groups of four characters. The secret is
        base32-encoded, so it is case insensitive."""
        sec = self.otp_secret
        chunks = [sec[i:i + 4] for i in xrange(0, len(sec), 4)]
        return ' '.join(chunks).lower()

    def _format_token(self, token):
        """Strips from authentication tokens the whitespace that many clients add for readability"""
        return ''.join(token.split())

    def verify_token(self, token):
        """Verify a HOTP/TOTP token (string), allowing for client-server
        skew. If `LOGIN_HARDENING` TOTP tokens may not be re-used, nor
        may the tokens preceding or proceeding a used token be used.

        Returns: bool
        Raises:
            BadTokenException: If `token` is not a six digit numeric string
                or is not valid for the given time window.
            TokenReuseException: If the token has been reused or precedes or
                proceeds a used token.
        """
        token = self._format_token(token)
        if not isinstance(token, str):
            raise BadTokenException("Token should be of type `str`.")
        if not len(token) == 6:
            raise BadTokenException("Token should be of `len` 6.")
        try:
            int(token)
        except ValueError:
            raise BadTokenException(
                "Token string should only contain digits.")

        def verify_hotp(token):
            """Check if a HOTP `token` is valid using current value of
            :attr:`self.hotp_counter`, as well as the next 19 tokens in
            case of client skew.

            Returns: bool
            """
            verified = False
            for counter_val in range(self.hotp_counter,
                                     self.hotp_counter + 20):
                if self.hotp.verify(token, counter_val):
                    verified = True
                    self.hotp_counter = counter_val + 1
                    db_session.add(self)
                    db_session.commit()
                    break
            return verified

        def check_token_reuse(token):
            """Check if a TOTP `token` has been used before, or if
            the `token` precedes or proceeds a token that has been used
            before.

            Returns: bool
            """
            timecode_last_access = self.totp.timecode(self.last_access)
            timecode_now = self.totp.timecode(datetime.datetime.utcnow())
            reused = False
            for timecode in range(timecode_last_access - 1,
                                  timecode_last_access + 2):
                reused |= pytop.utils.compare_digest(timecode_now,
                                                     timecode)
            return reused

        if self.is_totp:
            # Accept the tokens preceding and proceeding the current
            # token to account for client-server time skew.
            verified = self.totp.verify(token, valid_window=1)
            reused = check_token_reuse(token) if LOGIN_HARDENING else False
            if verified and reused:
                raise BadTokenException(
                    "Token valid, but reused, or token precedes or "
                    "proceeds a used token.")
            else:
                return verified and not reused
        else:
            return verify_hotp(token)

    @classmethod
    def throttle_login(cls, user):
        _LOGIN_ATTEMPT_PERIOD = 60  # seconds
        _MAX_LOGIN_ATTEMPTS_PER_PERIOD = 5

        # Record the login attempt...
        login_attempt = JournalistLoginAttempt(user)
        db_session.add(login_attempt)
        db_session.commit()

        # ...and reject it if they have exceeded the threshold
        login_attempt_period = datetime.datetime.utcnow() - \
            datetime.timedelta(seconds=_LOGIN_ATTEMPT_PERIOD)
        attempts_within_period = JournalistLoginAttempt.query.filter(
            JournalistLoginAttempt.timestamp > login_attempt_period).all()
        if len(attempts_within_period) > _MAX_LOGIN_ATTEMPTS_PER_PERIOD:
            raise LoginThrottledException(
                "throttled ({} attempts in last {} seconds)".format(
                    len(attempts_within_period),
                    _LOGIN_ATTEMPT_PERIOD))

    @classmethod
    def login(cls, username, password, token):
        """Takes a username, password, and OTP token (strings) and tries
        to authenticate the user. If `LOGIN_HARDENING` login attempts
        are rate-limited, and TOTP tokens which have already been used,
        or precede or proceed used tokens, are rejected.

        Returns: :obj:`Journalist`

        Raises:
            InvalidUsernameException: If no user can be found with the
                specified `username`.
            LoginThrottledException: If the user has exceeded the login
                rate-limiting threshold, or if the user is a TOTP user
                and has just successfully logged in within 60s.
            BadTokenException: If the `token` is invalid.
            TokenReuseException: If the `token` is a TOTP token that has
                already been used, or precedes or proceeds a used token.
            WrongPasswordException: If the user's `password` is invalid.
        """
        try:
            user = Journalist.query.filter_by(username=username).one()
        except NoResultFound:
            raise InvalidUsernameException(
                "invalid username '{}'".format(username))

        if LOGIN_HARDENING:
            cls.throttle_login(user)

        if not user.verify_token(token):
            raise BadTokenException("Invalid token.")
        if not user.valid_password(password):
            raise WrongPasswordException("invalid password")

        user.last_access = datetime.datetime.utcnow()
        db_session.add(user)
        db_session.commit()

        return user


class JournalistLoginAttempt(Base):

    """This model keeps track of journalist's login attempts so we can
    rate limit them in order to prevent attackers from brute forcing
    passwords or two factor tokens."""
    __tablename__ = "journalist_login_attempt"
    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)
    journalist_id = Column(Integer, ForeignKey('journalists.id'))

    def __init__(self, journalist):
        self.journalist_id = journalist.id


# Declare (or import) models before init_db
def init_db():
    Base.metadata.create_all(bind=engine)
