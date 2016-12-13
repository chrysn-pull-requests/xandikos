# Dystros
# Copyright (C) 2016 Jelmer Vernooij <jelmer@jelmer.uk>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; version 2
# of the License or (at your option) any later version of
# the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
# MA  02110-1301, USA.

"""Collections and collection sets."""

import os
import stat

from icalendar.cal import Calendar

from dulwich.objects import Blob, Tree
import dulwich.repo

_DEFAULT_COMMITTER_IDENTITY = b'Dystros <dystros>'


def ExtractUID(data):
    """Extract the UID from a VCalendar file.

    :param data: Serialized calendar.
    :return: UID
    """
    cal = Calendar.from_ical(data)
    for component in cal.subcomponents:
        try:
            return component["UID"]
        except KeyError:
            pass
    raise KeyError


class DuplicateUidError(Exception):
    """UID already exists in collection."""

    def __init__(self, uid, fname):
        self.uid = uid
        self.fname = fname


class NameExists(Exception):
    """Name exists."""

    def __init__(self, name):
        self.name = name


class NoSuchItem(Exception):
    """No such item."""

    def __init__(self, name):
        self.name = name


class InvalidETag(Exception):
    """Unexpected value for etag."""

    def __init__(self, name, expected_etag, got_etag):
        self.name = name
        self.expected_etag = expected_etag
        self.got_etag = got_etag


class Collection(object):
    """A ICalendar collection."""

    def iter_with_etag(self):
        """Iterate over all items in the collection with etag.

        :yield: (name, etag) tuples
        """
        raise NotImplementedError(self.iter_with_etag)

    def get_ctag(self):
        """Return the ctag for this collection."""
        raise NotImplementedError(self.get_ctag)

    def import_one(self, name, data):
        """Import a single VCalendar object.

        :param data: serialized vcalendar as bytes
        :raise NameExists: when the name already exists
        :raise DuplicateUidError: when the uid already exists
        :return: etag
        """
        raise NotImplementedError(self.import_one)

    def delete_one(self, name, etag=None):
        """Delete an item.

        :param name: Filename to delete
        :param etag: Optional mandatory etag of object to remove
        :raise NoSuchItem: when the item doesn't exist
        :raise InvalidETag: If the specified ETag doesn't match the current
        """
        raise NotImplementedError(self.delete_one)


class GitCollection(object):
    """A Collection backed by a Git Repository.
    """

    def __init__(self, repo, ref=b'refs/heads/master'):
        self.ref = ref
        self.repo = repo
        # Maps uids to (sha, fname)
        self._uid_to_fname = {}
        # Set of blob ids that have already been scanned
        self._fname_to_uid = {}

    def _check_duplicate_uid(self, uid):
        self._scan_ids()
        try:
            raise DuplicateUidError(uid, self._uid_to_fname[uid])
        except KeyError:
            pass

    def _check_duplicate_name(self, name):
        self._scan_ids()
        if name in self._fname_to_uid:
            raise NameExists(name)

    def iter_icalendars(self):
        """Iterate over all calendars.

        :yield: (name, Calendar) tuples
        """
        for (name, mode, sha) in self._iterblobs():
            yield (name, sha, Calendar.from_ical(self.repo.object_store[sha].data))

    def _scan_ids(self):
        removed = set(self._fname_to_uid.keys())
        for (name, mode, sha) in self._iterblobs():
            if name in removed:
                removed.remove(name)
            if (name in self._fname_to_uid and
                self._fname_to_uid[name][0] == sha):
                continue
            uid = ExtractUID(self.repo.object_store[sha].data)
            self._fname_to_uid[name] = (sha, uid)
            self._uid_to_fname[uid] = (sha, name)
        for name in removed:
            (sha, uid) = self._fname_to_uid[name]
            del self._uid_to_fname[uid]
            del self._fname_to_uid[name]

    def _iterblobs(self):
        raise NotImplementedError(self._iterblobs)

    def iter_with_etag(self):
        """Iterate over all items in the collection with etag.

        :yield: (name, etag) tuples
        """
        for (name, mode, sha) in self._iterblobs():
            yield (name, sha)

    @classmethod
    def create(cls, path):
        """Create a new collection backed by a Git repository on disk.

        :return: A `GitCollection`
        """
        raise NotImplementedError(self.create)

    @classmethod
    def open_from_path(cls, path):
        """Open a GitCollection from a path.

        :param path: Path
        :return: A `GitCollection`
        """
        return cls.open(dulwich.repo.Repo(path))

    @classmethod
    def open(cls, repo):
        """Open a GitCollection given a Repo object.

        :param repo: A Dulwich `Repo`
        :return: A `GitCollection`
        """
        if repo.has_index():
            return TreeGitCollection(repo)
        else:
            return BareGitCollection(repo)


class BareGitCollection(GitCollection):
    """A Collection backed by a bare git repository."""

    def _get_current_tree(self):
        try:
            ref_object = self.repo[self.ref]
        except KeyError:
            return Tree()
        if isinstance(ref_object, Tree):
            return ref_object
        else:
            return self.repo.object_store[ref_object.tree]

    def get_ctag(self):
        """Return the ctag for this collection."""
        return self._get_current_tree().id

    def _iterblobs(self):
        tree = self._get_current_tree()
        for (name, mode, sha) in tree.iteritems():
            yield (name.decode('utf-8'), mode, sha)

    @classmethod
    def create_memory(cls):
        """Create a new collection backed by a memory repository.

        :return: A `GitCollection`
        """
        return cls(dulwich.repo.MemoryRepo())

    def _commit_tree(self, tree_id, message):
        try:
            committer = self.repo._get_user_identity()
        except KeyError:
            committer = _DEFAULT_COMMITTER_IDENTITY
        return self.repo.do_commit(message=message, tree=tree_id,
                ref=self.ref, committer=committer)

    def import_one(self, name, data):
        """Import a single VCalendar object.

        :param data: serialized vcalendar as bytes
        :raise NameExists: when the name already exists
        :raise DuplicateUidError: when the uid already exists
        :return: etag
        """
        uid = ExtractUID(data)
        self._check_duplicate_uid(uid)
        self._check_duplicate_name(name)
        # TODO(jelmer): Handle case where the item already exists
        # TODO(jelmer): Verify that 'data' actually represents a valid calendar
        b = Blob.from_string(data)
        tree = self._get_current_tree()
        name_enc = name.encode('utf-8')
        tree.add(name_enc, 0o644|stat.S_IFREG, b.id)
        self.repo.object_store.add_objects([(tree, ''), (b, name_enc)])
        self._commit_tree(tree.id, b"Add " + name_enc)
        return b.id

    def delete_one(self, name, etag=None):
        """Delete an item.

        :param name: Filename to delete
        :param etag: Optional mandatory etag of object to remove
        :raise NoSuchItem: when the item doesn't exist
        :raise InvalidETag: If the specified ETag doesn't match the curren
        """
        tree = self._get_current_tree()
        name_enc = name.encode('utf-8')
        if not name_enc in tree:
            raise NoSuchItem(name)
        if etag is not None:
            current_etag = tree[name.encode('utf-8')][1]
            if current_etag != etag:
                raise InvalidETag(name, etag, current_etag)
        del tree[name]
        self._commit_tree(tree.id, b"Add " + name_enc)

    @classmethod
    def create(cls, path):
        """Create a new collection backed by a Git repository on disk.

        :return: A `GitCollection`
        """
        return cls(dulwich.repo.Repo.init_bare(path))


class TreeGitCollection(GitCollection):
    """A Collection that backs onto a treefull Git repository."""

    @classmethod
    def create(cls, path, bare=True):
        """Create a new collection backed by a Git repository on disk.

        :return: A `GitCollection`
        """
        return cls(dulwich.repo.Repo.init(path))

    def _commit_tree(self, message):
        try:
            committer = self.repo._get_user_identity()
        except KeyError:
            committer = _DEFAULT_COMMITTER_IDENTITY
        return self.repo.do_commit(message=message, committer=committer)

    def import_one(self, name, data):
        """Import a single VCalendar object.

        :param data: serialized vcalendar as bytes
        :raise NameExists: when the name already exists
        :raise DuplicateUidError: when the uid already exists
        :return: etag
        """
        uid = ExtractUID(data)
        self._check_duplicate_uid(uid)
        self._check_duplicate_name(name)
        # TODO(jelmer): Handle case where the item already exists
        # TODO(jelmer): Verify that 'data' actually represents a valid calendar
        p = os.path.join(self.repo.path, name)
        with open(p, 'wb') as f:
            f.write(data)
        self.repo.stage(name)
        etag = self.repo.open_index()[name.encode('utf-8')].sha
        message = b'Add ' + name.encode('utf-8')
        return etag

    def delete_one(self, name, etag=None):
        """Delete an item.

        :param name: Filename to delete
        :param etag: Optional mandatory etag of object to remove
        :raise NoSuchItem: when the item doesn't exist
        :raise InvalidETag: If the specified ETag doesn't match the curren
        """
        p = os.path.join(self.repo.path, name)
        if not os.path.exists(p):
            raise NoSuchItem(name)
        if etag is not None:
            with open(p, 'wb') as f:
                current_etag = Blob.from_string(f.read()).id
            if etag != current_etag:
                raise InvalidETag(name, etag, current_etag)
        os.unlink(p)
        self.repo.stage(name)

    def get_ctag(self):
        """Return the ctag for this collection."""
        index = self.repo.open_index()
        return index.commit(self.repo.object_store)

    def _iterblobs(self):
        """Iterate over all items in the collection with etag.

        :yield: (name, etag) tuples
        """
        index = self.repo.open_index()
        for (name, sha, mode) in index.iterblobs():
            yield (name.decode('utf-8'), mode, sha)


class CollectionSet(object):
    """A set of ICalendar collections.
    """


class FilesystemCollectionSet(object):
    """A CollectionSet that is backed by a filesystem."""

    def __init__(self, path):
        self._path = path