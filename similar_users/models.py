"""
Data models for the sockpuppet api.

Currently the schemas are a one-to-one mapping of fixtures produced
in development.
"""

from flask_sqlalchemy import SQLAlchemy

database = SQLAlchemy()


class UserMetadata(database.Model):
    """
    Represent attributes for users in Coedit.
    """
    __tablename__ = 'user'
    id = database.Column(database.Integer, primary_key=True, index=True)
    user_text = database.Column(database.String)
    is_anon = database.Column(database.Boolean)
    num_edits = database.Column(database.Integer)
    num_pages = database.Column(database.Integer)
    most_recent_edit = database.Column(database.DateTime)
    oldest_edit = database.Column(database.DateTime)


class Coedit(database.Model):
    """
    Represent a (user, user) similarity matrix in terms of number
    of edits in which two users overlapped.
    """
    __tablename__ = '__coedit__'
    id = database.Column(database.Integer, primary_key=True, index=True)
    user_text = database.Column(database.String)
    neighbor = database.Column(database.String)
    overlap_count = database.Column(database.Integer)


class Temporal(database.Model):
    """
    Represent temporal information about Coedit users editing behavioul - that is, when
    edit occurs.
    """
    __tablename__ = '__temporal__'
    id = database.Column(database.Integer, primary_key=True, index=True)
    user_text = database.Column(database.String)
    d = database.Column(database.Integer)
    h = database.Column(database.Integer)