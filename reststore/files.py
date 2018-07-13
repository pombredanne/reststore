import os
import re
import hashlib
import tempfile
import sqlite3
import math
import random
import time

from reststore import config


FILES_TABLE = "\
CREATE TABLE IF NOT EXISTS files (\
 hexdigest varchar,\
 filepath varchar,\
 created datetime DEFAULT current_timestamp);"
FILES_HEXDIGEST_IDX = "\
CREATE UNIQUE INDEX files_hexdigest_idx on files(hexdigest);"
SELECT_ROWIDS = "SELECT MIN(rowid), MAX(rowid) from files;"
DELETE_TO_ROWID = "DELETE FROM files where rowid<=%s"
SELECT_FILEPATH = "SELECT filepath from files where hexdigest='%s'"
INSERT_HEXDIGEST = "INSERT INTO files (hexdigest) values ('%s')"
UPDATE_FILEPATH = "UPDATE files set filepath='%s' where hexdigest='%s'"
SELECT_FILEPATH_HEXDIGEST = "\
SELECT hexdigest, filepath, rowid from files ORDER BY rowid LIMIT %s"
SELECT_DIGESTS_LIMIT = "SELECT hexdigest from files ORDER BY rowid LIMIT %s OFFSET %s"
SELECT_DIGESTS = "SELECT hexdigest from files ORDER BY rowid"


class DataError(Exception): pass


class Files:
    def __init__(self, name=None, files_root=None, hash_func=None,
                 tune_size=None, assert_data_ok=None):
        """Create a Files interface object.  

        This object is responsible for putting and getting data from the files
        data store.  

        Optional keyword arguments:
            name - The name applied to this data store.  This name will be
                used as the root folder name for accessing the data store.
            files_root - This is the root folder in which data stores are
                created.
            hash_func - The string value name for the hashing algorithm to
                apply to a specific 'name' data store. The name of the hashing
                algorithm needs to exist in Python's default hashlib module. 
            tune_size - This value is used to tune an optimised shape of the
                data store on disk.  It is best to set this value to a value
                you expect the datastore could grow to.
            assert_data_ok - If this flag is True, additional checks are made
                when data is accessed from disk.  This may not be desirable as
                it adds additional checks which may cause undesirable overheads
                in execution.        

        Note: Default values for the parameters in this of this constructor
              are defined and set in reststore.config
        """
        files_config = config.values['files']
        name = name or files_config['name']
        files_root = files_root or files_config['root']
        hash_func = hash_func or files_config['hash_function']
        tune_size = tune_size or files_config['tune_size']
        assert_data_ok = assert_data_ok or files_config['assert_data_ok']
        if os.path.sep in name or '..' in name:
            raise ValueError('name can not contain .. or %s' % os.path.sep)
        self.hash_func = getattr(hashlib, hash_func)
        self.hash_len = len(self.hash_func('').hexdigest())
        self._root = os.path.join(files_root, name)
        #create our db if it doesn't exist already
        self._db = os.path.join(self._root, 'files.db')
        if not os.path.exists(self._db):
            #new repo...  lets create it
            if not os.path.exists(self._root):
                os.makedirs(self._root)
            con = sqlite3.connect(self._db)
            con.execute(FILES_TABLE)
            con.execute(FILES_HEXDIGEST_IDX)
            con.commit()
            self.index = 0
        self._folder_width = math.ceil(math.pow(tune_size, 1.0/3))
        self._folder_fmt = "%%0%sd" % len(str(self._folder_width))
        self._do_assert_data_ok = assert_data_ok

    def __len__(self):
        con = sqlite3.connect(self._db)
        c = con.execute(SELECT_ROWIDS)
        res = c.fetchone()
        if res[0] is None:
            return 0
        start, end = res
        return end - start + 1
    
    def get(self, hexdigest, d=None):
        """Get a filepath for the data corresponding to the hexdigest"""
        try:
            return self[hexdigest]
        except KeyError:
            return d

    def _assert_data_ok(self, hexdigest, filepath):
        if os.path.exists(filepath) is False:
            raise DataError('%s not found' % filepath)
        with open(filepath) as f:
            data = f.read()
        actual = self.hash_func(data).hexdigest()
        if hexdigest != actual:
            raise DataError('Expected %s, got %s' % (hexdigest, actual))

    def __getitem__(self, hexdigest):
        con = sqlite3.connect(self._db)
        c = con.execute(SELECT_FILEPATH % hexdigest)
        res = c.fetchone()
        if res is None:
            raise KeyError('%s not found' % hexdigest)
        filepath = os.path.join(self._root, res[0])
        if self._do_assert_data_ok:
            self._assert_data_ok(hexdigest, filepath)          
        return filepath

    def __contains__(self, hexdigest):
        try:
            self[hexdigest]
        except Exception:
            return False
        return True

    def __setitem__(self, hexdigest, data):
        self.put(data, hexdigest=hexdigest)
    
    def put(self, data, hexdigest=None):
        """Puts data into the data store. Returns the hexdigest for the data""" 
        if hexdigest is None:
            hexdigest = self.hash_func(data).hexdigest()
        else:
            hexdigest = hexdigest.lower()
            actual = self.hash_func(data).hexdigest()
            if hexdigest != actual:
                raise ValueError('actual hash %s != hexdigest %s' % \
                                    (actual, hexdigest))
        #check if it already exists
        try:
            self[hexdigest]
        except (KeyError, DataError):
            pass
        else:
            return hexdigest

        attempts = 0
        filepath = ''
        l1 = random.randint(0, self._folder_width)
        l2 = random.randint(0, self._folder_width)
        while True:
            # create our entry
            try:
                # would be good to reduce transaction scope until after
                # file is written but relies on rowid for filename...
                con = sqlite3.connect(self._db)
                c = con.execute(INSERT_HEXDIGEST % hexdigest)
                rowid = c.lastrowid
                relroot = os.path.join(self._folder_fmt%l1,
                            self._folder_fmt%l2)
                path = os.path.join(relroot, str(rowid))
                c = con.execute(UPDATE_FILEPATH % (path, hexdigest))
                dirpath = os.path.join(self._root, relroot)
                if not os.path.exists(dirpath):
                    os.makedirs(dirpath)
                filepath = os.path.join(self._root, path)
                with open(filepath, 'wb') as f:
                    f.write(data)
                con.commit()
                break
            # handle concurrent race/lock errors on insert
            except sqlite3.DatabaseError, ex:
                attempts += 1
                # remove any partial attempt
                try:
                    os.remove(filepath)
                except OSError:
                    pass
                # did someone else succeed?
                if self.get(hexdigest):
                    break
                # give up
                if attempts >= 10:
                    raise ex
                time.sleep(0.2)

        return hexdigest

    def bulk_put(self, data, hexdigest=None):
        return self.put(data, hexdigest=hexdigest)

    def bulk_flush(self):
        return

    def __iter__(self):
        con = sqlite3.connect(self._db)
        c = con.execute(SELECT_DIGESTS)
        while True:
            rows = c.fetchmany()
            if not rows:
                break
            for row in rows:
                hexdigest = row[0]
                yield hexdigest

    def select(self, a, b):
        """Select a range of hexdigest values to return.
        
        This function provides a way to "slice" the data entered into this
        store.  
        Examples:
            select(-2, -1) return the hexdigest for the last data entered into
                           the store.
            select(0, 1)   return the first hexdigest for the first data
                           entered, 
            select(10,-10) will return all of the hexdigests between the 10th
                           inserted and the 10th last inserted data.

        Return a list of hexdigests
        """
        if a < 0:
            a = len(self) + a + 1
        if b < 0:
            b = len(self) + b + 1
        if b < a:
            a, b = b, a
        limit = b-a
        offset = a

        con = sqlite3.connect(self._db)
        c = con.execute(SELECT_DIGESTS_LIMIT % (limit, offset))
        rows = c.fetchall()
        hexdigests = [row[0] for row in rows]
        return hexdigests

    def expire(self, count):
        """Drop the oldest `count` files from the reststore"""
        with sqlite3.connect(self._db) as con:
            c = con.execute(SELECT_FILEPATH_HEXDIGEST % count)
            rows = c.fetchall()
            if not rows:
                return
            # delete files and truncate db to same point
            for hexdigest, path, rowid in rows:
                filepath = os.path.join(self._root, path)
                try:
                    os.remove(filepath)
                except OSError:
                    # We _could_ break here as most likely cause is that
                    # a concurrent proc has hit the same path.. however,
                    # if there is a chance that the file could be deleted
                    # externally we are safest to just continue and remove
                    # the db reference, else cache cleaning would get stuck.
                    # Downside is multiple procs could be wasting their time.
                    pass

            con.execute(DELETE_TO_ROWID % rowid)

