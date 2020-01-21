#!/usr/bin/env python
#
# Public Domain 2014-2019 MongoDB, Inc.
# Public Domain 2008-2014 WiredTiger, Inc.
#
# This is free and unencumbered software released into the public domain.
#
# Anyone is free to copy, modify, publish, use, compile, sell, or
# distribute this software, either in source code form or as a compiled
# binary, for any purpose, commercial or non-commercial, and by any
# means.
#
# In jurisdictions that recognize copyright laws, the author or authors
# of this software dedicate any and all copyright interest in the
# software to the public domain. We make this dedication for the benefit
# of the public at large and to the detriment of our heirs and
# successors. We intend this dedication to be an overt act of
# relinquishment in perpetuity of all present and future rights to this
# software under copyright law.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# IN NO EVENT SHALL THE AUTHORS BE LIABLE FOR ANY CLAIM, DAMAGES OR
# OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
# ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.

import time
from helper import copy_wiredtiger_home
import unittest, wiredtiger, wttest
from wtdataset import SimpleDataSet
from test_gc01 import test_gc_base
from wiredtiger import stat

def timestamp_str(t):
    return '%x' % t

# test_gc03.py
# Test that checkpoint cleans the obsolete history store pages that are in-memory.
class test_gc03(test_gc_base):
    conn_config = 'cache_size=4GB,log=(enabled),statistics=(all),statistics_log=(wait=0,on_close=true)'
    session_config = 'isolation=snapshot'

    def large_updates(self, uri, value, ds, nrows, commit_ts):
        # Update a large number of records.
        session = self.session
        cursor = session.open_cursor(uri)
        for i in range(1, nrows + 1):
            session.begin_transaction()
            cursor[ds.key(i)] = value
            session.commit_transaction('commit_timestamp=' + timestamp_str(commit_ts))
        cursor.close()

    def large_modifies(self, uri, value, ds, location, nbytes, nrows, commit_ts):
        # Load a slight modification with a later timestamp.
        session = self.session
        cursor = session.open_cursor(uri)
        session.begin_transaction()
        for i in range(1, nrows):
            cursor.set_key(i)
            mods = [wiredtiger.Modify(value, location, nbytes)]
            self.assertEqual(cursor.modify(mods), 0)
        session.commit_transaction('commit_timestamp=' + timestamp_str(commit_ts))
        cursor.close()

    def check(self, check_value, uri, nrows, read_ts):
        session = self.session
        session.begin_transaction('read_timestamp=' + timestamp_str(read_ts))
        cursor = session.open_cursor(uri)
        count = 0
        for k, v in cursor:
            self.assertEqual(v, check_value)
            count += 1
        session.rollback_transaction()
        self.assertEqual(count, nrows)

    def test_gc(self):
        nrows = 10000

        # Create a table without logging.
        uri = "table:gc03"
        ds = SimpleDataSet(
            self, uri, 0, key_format="i", value_format="S", config='log=(enabled=false)')
        ds.populate()

        # Create an extra table without logging.
        uri_extra = "table:gc03_extra"
        ds_extra = SimpleDataSet(
            self, uri_extra, 0, key_format="i", value_format="S", config='log=(enabled=false)')
        ds_extra.populate()

        # open the stats cursor
        stat_cursor = self.session.open_cursor('statistics:', None, 'statistics=(fast)')

        # Pin oldest and stable to timestamp 1.
        self.conn.set_timestamp('oldest_timestamp=' + timestamp_str(1) +
            ',stable_timestamp=' + timestamp_str(1))

        bigvalue = "aaaaa" * 100
        bigvalue2 = "ddddd" * 100
        self.large_updates(uri, bigvalue, ds, nrows, 10)

        # Check that all updates are seen
        #self.check(bigvalue, uri, nrows, 10)

        self.large_updates(uri, bigvalue2, ds, nrows, 100)

        # Check that the new updates are only seen after the update timestamp
        #self.check(bigvalue2, uri, nrows, 100)

        # Check that old updates are seen
        #self.check(bigvalue, uri, nrows, 10)

        # Checkpoint to ensure that the history store is gets populated
        self.session.checkpoint()

        # Pin oldest and stable to timestamp 100.
        self.conn.set_timestamp('oldest_timestamp=' + timestamp_str(100) +
            ',stable_timestamp=' + timestamp_str(100))

        # Check that the new updates are only seen after the update timestamp
        #self.check(bigvalue2, uri, nrows, 100)

        # Load a slight modification with a later timestamp.
        self.large_modifies(uri, 'A', ds, 10, 1, nrows, 110)
        self.large_modifies(uri, 'B', ds, 20, 1, nrows, 120)
        self.large_modifies(uri, 'C', ds, 30, 1, nrows, 130)

        # Second set of update operations with increased timestamp
        self.large_updates(uri, bigvalue, ds, nrows, 200)

        # Check that the new updates are only seen after the update timestamp
        #self.check(bigvalue, uri, nrows, 200)

        # Check that old updates are seen
        #self.check(bigvalue2, uri, nrows, 100)

        # Pin oldest and stable to timestamp 200.
        self.conn.set_timestamp('oldest_timestamp=' + timestamp_str(200) +
            ',stable_timestamp=' + timestamp_str(200))

        # update on extra table
        self.large_updates(uri_extra, bigvalue, ds_extra, 100, 210)
        self.large_updates(uri_extra, bigvalue2, ds_extra, 100, 220)

        # Checkpoint to ensure that the history store is gets populated and added for eviction
        self.session.checkpoint()
        #self.assertGreater(stat_cursor[stat.conn.hs_gc_pages_evict][2], 0)
        #self.assertGreater(stat_cursor[stat.conn.hs_gc_pages_visited][2], 0)

        # Check that the new updates are only seen after the update timestamp
        #self.check(bigvalue, uri, nrows, 200)

        # Pin oldest and stable to timestamp 300.
        self.conn.set_timestamp('oldest_timestamp=' + timestamp_str(300) +
            ',stable_timestamp=' + timestamp_str(300))

        self.large_updates(uri_extra, bigvalue, ds_extra, 100, 310)
        self.large_updates(uri_extra, bigvalue2, ds_extra, 100, 320)

        # Checkpoint to ensure that the normal table history store gets cleaned
        self.session.checkpoint()
        self.check_gc_stats()

        # Check that the new updates are only seen after the update timestamp
        #self.check(bigvalue, uri, nrows, 300)

if __name__ == '__main__':
    wttest.run()
