import os
import base64

import onetimepass
import scrypt

import cryptography.exceptions
from cryptography.hazmat.primitives.constant_time import bytes_eq
from cryptography.hazmat.primitives.ciphers.aead import AESCCM

from flask_login import UserMixin


def create_user_database_model(database):
    class User(UserMixin, database.Model):
        __tablename__ = 'users'

        id = database.Column(database.Integer, primary_key=True)
        username = database.Column(database.String(64), index=True, unique=True)

        salt = database.Column(database.String(56))
        password_hash = database.Column(database.String(128))

        tfa_secret = database.Column(database.String(16))
        tfa_active = database.Column(database.Integer)

        role = database.Column(database.String(15))

        def __init__(self, **kwargs):
            super(User, self).__init__(**kwargs)

            # Password salt
            if self.salt is None:
                self.salt = base64.b32encode(os.urandom(32)).decode('utf-8')

            if self.role is None:
                self.role = 'admin'

            if self.tfa_secret is None:
                self.tfa_secret = base64.b32encode(os.urandom(10)).decode('utf-8')

            if self.tfa_active is None:
                self.tfa_active = 0

        @property
        def password(self):
            raise AttributeError('password is not a readable attribute')

        @password.setter
        def password(self, password):
            if self.salt is None:
                self.salt = base64.b32encode(os.urandom(32)).decode('utf-8')

            self.password_hash = base64.b64encode(scrypt.hash(base64.b64decode(self.salt), password, buflen=128)).decode('utf-8')

        def verify_password(self, password):
            return bytes_eq(scrypt.hash(base64.b64decode(self.salt), password, buflen=128),
                            base64.b64decode(self.password_hash.encode()))

        def get_totp_uri(self):
            return 'otpauth://totp/Liquitrader:{0}?secret={1}&issuer=Liquitrader'.format(self.username, self.tfa_secret)

        def verify_totp(self, token):
            return onetimepass.valid_totp(token, self.tfa_secret)

    return User


def create_keystore_database_model(database):
    class KeyStore(database.Model):
        __tablename__ = 'keystore'

        id = database.Column(database.Integer, primary_key=True)

        license = database.Column(database.String(50))
        exchange_key_public = database.Column(database.String(50))
        exchange_key_private = database.Column(database.String(50))

        master_key = database.Column(database.String(60))
        master_nonce = database.Column(database.String(30))

        _flask_secret = database.Column(database.String(32))

        def __init__(self, **kwargs):
            super(KeyStore, self).__init__(**kwargs)

            if self.master_key is None:
                self.master_key = self._to_b32(AESCCM.generate_key(256))

            if self.master_nonce is None:
                self.master_nonce = self._to_b32(os.urandom(13))

            if self._flask_secret is None:
                self._flask_secret = self._encrypt(os.urandom(16))

        # ----
        def _to_b32(self, data):
            return base64.b32encode(data).decode('utf-8')

        # --
        def _from_b32(self, data):
            return base64.b32decode(data.encode())

        # ----
        def _get_engine(self):
            return AESCCM(self._from_b32(self.master_key))

        # ----
        def _encrypt(self, data):
            engine = self._get_engine()
            master_nonce = self._from_b32(self.master_nonce)

            if type(data) == str:
                data = bytes(data, 'utf-8')

            return self._to_b32(engine.encrypt(master_nonce, data, b'\xd7\x83>}\xc4<\xcd\xfd+?'))

        # --
        def _decrypt(self, data):
            engine = self._get_engine()
            master_nonce = self._from_b32(self.master_nonce)
            data = self._from_b32(data)

            # Each assoc_data is 10 bytes generated by os.urandom
            # This allows us to roll out new encryption keys periodically to help make script kiddie's lives harder
            # Newest key should be added to the end of the list. Make sure to update the value is '_encrypt'.
            assoc_data = [b'\xd7\x83>}\xc4<\xcd\xfd+?']

            attempts = 0
            dec = None
            working_dat = None
            for dat in reversed(assoc_data):
                attempts += 1

                try:
                    dec = engine.decrypt(master_nonce, data, dat)
                except cryptography.exceptions.InvalidTag:
                    continue

                working_dat = dat

            if dec is None or working_dat is None:
                raise ValueError('Critical error: Database is corrupted')

            # This looks weird, but they are actually re-encrypted here due to the way properties work
            if attempts > 1:
                self.private_exchange_key = engine.decrypt(master_nonce, self.exchange_key_private, working_dat)
                self.public_exchange_key = engine.decrypt(master_nonce, self.exchange_key_public, working_dat)
                self._flask_secret = self._encrypt(engine.decrypt(master_nonce, self._flask_secret, working_dat))

            return dec

        # ----
        @property
        def public_exchange_key(self):
            # Decrypt public key and return
            return str(self._decrypt(self.exchange_key_public))[2:-1]

        # --
        @public_exchange_key.setter
        def public_exchange_key(self, value):
            # Encrypt public key and store
            self.exchange_key_public = self._encrypt(value)

        # ----
        @property
        def private_exchange_key(self):
            # Decrypt private key and return
            return str(self._decrypt(self.exchange_key_private))[2:-1]

        # --
        @private_exchange_key.setter
        def private_exchange_key(self, value):
            # Encrypt private key and store
            self.exchange_key_private = self._encrypt(value)

        # ----
        @property
        def liquitrader_license_key(self):
            # Decrypt private key and return
            return str(self._decrypt(self.license))[2:-1]

        # --
        @liquitrader_license_key.setter
        def liquitrader_license_key(self, value):
            # Encrypt LiquiTrader license and store
            self.license = self._encrypt(value)

        # --
        @property
        def flask_secret(self):
            return str(self._decrypt(self._flask_secret))[2:-1]

    return KeyStore


def migrate_table(database):
    user_column_defs = ', '.join([
        'id INTEGER NOT NULL',
        'username VARCHAR(64) NOT NULL',
        'salt VARCHAR(56) NOT NULL',
        'password_hash VARCHAR(128) NOT NULL',
        'tfa_active INTEGER',
        'tfa_secret VARCHAR(16)',
        'role VARCHAR(15)',
        'PRIMARY KEY (id)'
    ])

    # role = database.Column(database.String(15), index=True)
    # version = database.Column(database.Integer)

    # TODO: ADD ROLE TO USERS TABLE

    with database.engine.begin() as conn:
        existing_cols = [_[1] for _ in conn.execute('PRAGMA table_info(users)').fetchall()]

        if 'role' in existing_cols:  # This is a current (8/20/18) indication of an up-to-date database
            return

        conn.execute('ALTER TABLE users RENAME TO _users_old')

        conn.execute('CREATE TABLE users ( {} )'.format(user_column_defs))

        if 'tfa_active' in existing_cols:
            conn.execute('INSERT INTO users ( id, username, password_hash, tfa_active, tfa_secret, role ) '
                             'SELECT id, username, password_hash, tfa_active, tfa_secret, "admin" '
                             'FROM _users_old'
                         )

        else:
            conn.execute('INSERT INTO users ( id, username, password_hash, tfa_active, role ) '
                            'SELECT id, username, password_hash, 0, "admin" '
                            'FROM _users_old'
                         )

        conn.execute('DROP TABLE _users_old')