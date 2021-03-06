# -*- coding: utf-8 -*-
import os
import unittest

from flask import current_app

os.environ['SECUREDROP_ENV'] = 'test'  # noqa
from sdconfig import config
import crypto_util
import journalist_app
import models
import utils

from crypto_util import CryptoUtil, CryptoException
from db import db


class TestCryptoUtil(unittest.TestCase):

    """The set of tests for crypto_util.py."""

    def setUp(self):
        self.__context = journalist_app.create_app(config).app_context()
        self.__context.push()
        utils.env.setup()

    def tearDown(self):
        utils.env.teardown()
        self.__context.pop()

    def test_word_list_does_not_contain_empty_strings(self):
        self.assertNotIn('', (current_app.crypto_util.get_wordlist('en')
                              + current_app.crypto_util.nouns
                              + current_app.crypto_util.adjectives))

    def test_clean(self):
        ok = (' !#%$&)(+*-1032547698;:=?@acbedgfihkjmlonqpsrutwvyxzABCDEFGHIJ'
              'KLMNOPQRSTUVWXYZ')
        invalid_1 = 'foo bar`'
        invalid_2 = 'bar baz~'

        self.assertEqual(ok, crypto_util.clean(ok))
        with self.assertRaisesRegexp(CryptoException,
                                     'invalid input: {}'.format(invalid_1)):
            crypto_util.clean(invalid_1)
        with self.assertRaisesRegexp(CryptoException,
                                     'invalid input: {}'.format(invalid_2)):
            crypto_util.clean(invalid_2)

    def test_encrypt_success(self):
        source, _ = utils.db_helper.init_source()
        message = str(os.urandom(1))
        ciphertext = current_app.crypto_util.encrypt(
            message,
            [current_app.crypto_util.getkey(source.filesystem_id),
             config.JOURNALIST_KEY],
            current_app.storage.path(source.filesystem_id, 'somefile.gpg'))

        self.assertIsInstance(ciphertext, str)
        self.assertNotEqual(ciphertext, message)
        self.assertGreater(len(ciphertext), 0)

    def test_encrypt_failure(self):
        source, _ = utils.db_helper.init_source()
        with self.assertRaisesRegexp(CryptoException,
                                     'no terminal at all requested'):
            current_app.crypto_util.encrypt(
                str(os.urandom(1)),
                [],
                current_app.storage.path(source.filesystem_id, 'other.gpg'))

    def test_encrypt_without_output(self):
        """We simply do not specify the option output keyword argument
        to crypto_util.encrypt() here in order to confirm encryption
        works when it defaults to `None`.
        """
        source, codename = utils.db_helper.init_source()
        message = str(os.urandom(1))
        ciphertext = current_app.crypto_util.encrypt(
            message,
            [current_app.crypto_util.getkey(source.filesystem_id),
             config.JOURNALIST_KEY])
        plaintext = current_app.crypto_util.decrypt(codename, ciphertext)

        self.assertEqual(message, plaintext)

    def test_encrypt_binary_stream(self):
        """Generally, we pass unicode strings (the type form data is
        returned as) as plaintext to crypto_util.encrypt(). These have
        to be converted to "binary stream" types (such as `file`) before
        we can actually call gnupg.GPG.encrypt() on them. This is done
        in crypto_util.encrypt() with an `if` branch that uses
        `gnupg._util._is_stream(plaintext)` as the predicate, and calls
        `gnupg._util._make_binary_stream(plaintext)` if necessary. This
        test ensures our encrypt function works even if we provide
        inputs such that this `if` branch is skipped (i.e., the object
        passed for `plaintext` is one such that
        `gnupg._util._is_stream(plaintext)` returns `True`).
        """
        source, codename = utils.db_helper.init_source()
        with open(os.path.realpath(__file__)) as fh:
            ciphertext = current_app.crypto_util.encrypt(
                fh,
                [current_app.crypto_util.getkey(source.filesystem_id),
                 config.JOURNALIST_KEY],
                current_app.storage.path(source.filesystem_id, 'somefile.gpg'))
        plaintext = current_app.crypto_util.decrypt(codename, ciphertext)

        with open(os.path.realpath(__file__)) as fh:
            self.assertEqual(fh.read(), plaintext)

    def test_encrypt_fingerprints_not_a_list_or_tuple(self):
        """If passed a single fingerprint as a string, encrypt should
        correctly place that string in a list, and encryption/
        decryption should work as intended."""
        source, codename = utils.db_helper.init_source()
        message = str(os.urandom(1))
        ciphertext = current_app.crypto_util.encrypt(
            message,
            current_app.crypto_util.getkey(source.filesystem_id),
            current_app.storage.path(source.filesystem_id, 'somefile.gpg'))
        plaintext = current_app.crypto_util.decrypt(codename, ciphertext)

        self.assertEqual(message, plaintext)

    def test_basic_encrypt_then_decrypt_multiple_recipients(self):
        source, codename = utils.db_helper.init_source()
        message = str(os.urandom(1))
        ciphertext = current_app.crypto_util.encrypt(
            message,
            [current_app.crypto_util.getkey(source.filesystem_id),
             config.JOURNALIST_KEY],
            current_app.storage.path(source.filesystem_id, 'somefile.gpg'))
        plaintext = current_app.crypto_util.decrypt(codename, ciphertext)

        self.assertEqual(message, plaintext)

        # Since there's no way to specify which key to use for
        # decryption to python-gnupg, we delete the `source`'s key and
        # ensure we can decrypt with the `config.JOURNALIST_KEY`.
        current_app.crypto_util.delete_reply_keypair(source.filesystem_id)
        plaintext_ = current_app.crypto_util.gpg.decrypt(ciphertext).data

        self.assertEqual(message, plaintext_)

    def verify_genrandomid(self, locale):
        id = current_app.crypto_util.genrandomid(locale=locale)
        id_words = id.split()

        self.assertEqual(id, crypto_util.clean(id))
        self.assertEqual(len(id_words), CryptoUtil.DEFAULT_WORDS_IN_RANDOM_ID)
        for word in id_words:
            self.assertIn(word, current_app.crypto_util.get_wordlist(locale))

    def test_genrandomid_default_locale_is_en(self):
        self.verify_genrandomid('en')

    def test_get_wordlist(self):
        locales = []
        wordlists_path = os.path.join(config.SECUREDROP_ROOT, 'wordlists')
        for f in os.listdir(wordlists_path):
            if f.endswith('.txt') and f != 'en.txt':
                locales.append(f.split('.')[0])
        wordlist_en = current_app.crypto_util.get_wordlist('en')
        for locale in locales:
            self.assertNotEqual(wordlist_en,
                                current_app.crypto_util.get_wordlist(locale))
            self.verify_genrandomid(locale)
        self.assertEqual(wordlist_en,
                         current_app.crypto_util.get_wordlist('unknown'))

    def test_display_id(self):
        id = current_app.crypto_util.display_id()
        id_words = id.split()

        self.assertEqual(len(id_words), 2)
        self.assertIn(id_words[0], current_app.crypto_util.adjectives)
        self.assertIn(id_words[1], current_app.crypto_util.nouns)

    def test_hash_codename(self):
        codename = current_app.crypto_util.genrandomid()
        hashed_codename = current_app.crypto_util.hash_codename(codename)

        self.assertRegexpMatches(hashed_codename, '^[2-7A-Z]{103}=$')

    def test_genkeypair(self):
        codename = current_app.crypto_util.genrandomid()
        filesystem_id = current_app.crypto_util.hash_codename(codename)
        journalist_filename = current_app.crypto_util.display_id()
        source = models.Source(filesystem_id, journalist_filename)
        db.session.add(source)
        db.session.commit()
        current_app.crypto_util.genkeypair(source.filesystem_id, codename)

        self.assertIsNotNone(current_app.crypto_util.getkey(filesystem_id))

    def test_delete_reply_keypair(self):
        source, _ = utils.db_helper.init_source()
        current_app.crypto_util.delete_reply_keypair(source.filesystem_id)

        self.assertIsNone(current_app.crypto_util.getkey(source.filesystem_id))

    def test_delete_reply_keypair_no_key(self):
        """No exceptions should be raised when provided a filesystem id that
        does not exist.
        """
        current_app.crypto_util.delete_reply_keypair('Reality Winner')

    def test_getkey(self):
        source, _ = utils.db_helper.init_source()

        self.assertIsNotNone(
            current_app.crypto_util.getkey(source.filesystem_id))
