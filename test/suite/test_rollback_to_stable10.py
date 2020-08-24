#!/usr/bin/env python
#
# Public Domain 2014-2020 MongoDB, Inc.
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

import fnmatch, os, shutil, threading, time
from helper import copy_wiredtiger_home
from test_rollback_to_stable01 import test_rollback_to_stable_base
from wiredtiger import stat
from wtdataset import SimpleDataSet
from wtscenario import make_scenarios
from wtthread import checkpoint_thread, op_thread

def timestamp_str(t):
    return '%x' % t

# test_rollback_to_stable10.py
# Test the rollback to stable operation performs sweeping history store.
class test_rollback_to_stable10(test_rollback_to_stable_base):
    session_config = 'isolation=snapshot'

    prepare_values = [
        ('no_prepare', dict(prepare=False)),
        ('prepare', dict(prepare=True))
    ]

    scenarios = make_scenarios(prepare_values)

    def conn_config(self):
        config = 'cache_size=6MB,statistics=(all),statistics_log=(json,on_close,wait=1),log=(enabled=true),timing_stress_for_test=[history_store_checkpoint_delay]'
        return config

    def test_rollback_to_stable(self):
        nrows = 1000

        # Create a table without logging.
        uri_1 = "table:rollback_to_stable10_1"
        ds_1 = SimpleDataSet(
            self, uri_1, 0, key_format="i", value_format="S", config='log=(enabled=false)')
        ds_1.populate()

        # Create another table without logging.
        uri_2 = "table:rollback_to_stable10_2"
        ds_2 = SimpleDataSet(
            self, uri_2, 0, key_format="i", value_format="S", config='log=(enabled=false)')
        ds_2.populate()

        # Pin oldest and stable to timestamp 10.
        self.conn.set_timestamp('oldest_timestamp=' + timestamp_str(10) +
            ',stable_timestamp=' + timestamp_str(10))

        value_a = "aaaaa" * 100
        value_b = "bbbbb" * 100
        value_c = "ccccc" * 100
        value_d = "ddddd" * 100
        value_e = "eeeee" * 100
        value_f = "fffff" * 100

        # Perform several updates.
        self.large_updates(uri_1, value_d, ds_1, nrows, 20)
        self.large_updates(uri_1, value_c, ds_1, nrows, 30)
        self.large_updates(uri_1, value_b, ds_1, nrows, 40)
        self.large_updates(uri_1, value_a, ds_1, nrows, 50)

        self.large_updates(uri_2, value_d, ds_2, nrows, 20)
        self.large_updates(uri_2, value_c, ds_2, nrows, 30)
        self.large_updates(uri_2, value_b, ds_2, nrows, 40)
        self.large_updates(uri_2, value_a, ds_2, nrows, 50)

        # Verify data is visible and correct.
        self.check(value_d, uri_1, nrows, 20)
        self.check(value_c, uri_1, nrows, 30)
        self.check(value_b, uri_1, nrows, 40)
        self.check(value_a, uri_1, nrows, 50)

        self.check(value_d, uri_2, nrows, 20)
        self.check(value_c, uri_2, nrows, 30)
        self.check(value_b, uri_2, nrows, 40)
        self.check(value_a, uri_2, nrows, 50)

        # Pin stable to timestamp 60 if prepare otherwise 50.
        if self.prepare:
            self.conn.set_timestamp('stable_timestamp=' + timestamp_str(60))
        else:
            self.conn.set_timestamp('stable_timestamp=' + timestamp_str(50))

        # Create a checkpoint thread
        done = threading.Event()
        ckpt = checkpoint_thread(self.conn, done)
        try:
            ckpt.start()

            # Perform several updates in parallel with checkpoint.
            self.large_updates(uri_1, value_e, ds_1, nrows, 70)
            self.large_updates(uri_2, value_e, ds_2, nrows, 70)
            self.large_updates(uri_1, value_f, ds_1, nrows, 80)
            self.large_updates(uri_2, value_f, ds_2, nrows, 80)
        finally:
            done.set()
            ckpt.join()

        # Simulate a server crash and restart.
        self.simulate_crash_restart(".", "RESTART")

        # Check that the correct data is seen at and after the stable timestamp.
        self.check(value_a, uri_1, nrows, 50)
        self.check(value_a, uri_1, nrows, 80)
        self.check(value_b, uri_1, nrows, 40)
        self.check(value_c, uri_1, nrows, 30)
        self.check(value_d, uri_1, nrows, 20)

        # Check that the correct data is seen at and after the stable timestamp.
        self.check(value_c, uri_2, nrows, 30)
        self.check(value_a, uri_2, nrows, 50)
        self.check(value_a, uri_2, nrows, 80)
        self.check(value_b, uri_2, nrows, 40)
        self.check(value_d, uri_2, nrows, 20)

        stat_cursor = self.session.open_cursor('statistics:', None, None)
        calls = stat_cursor[stat.conn.txn_rts][2]
        hs_removed = stat_cursor[stat.conn.txn_rts_hs_removed][2]
        hs_sweep = stat_cursor[stat.conn.txn_rts_sweep_hs_keys][2]
        keys_removed = stat_cursor[stat.conn.txn_rts_keys_removed][2]
        keys_restored = stat_cursor[stat.conn.txn_rts_keys_restored][2]
        pages_visited = stat_cursor[stat.conn.txn_rts_pages_visited][2]
        upd_aborted = stat_cursor[stat.conn.txn_rts_upd_aborted][2]
        stat_cursor.close()

        self.assertEqual(calls, 0)
        self.assertEqual(keys_removed, 0)
        self.assertEqual(keys_restored, 0)
        self.assertGreaterEqual(upd_aborted, 0)
        self.assertGreater(pages_visited, 0)
        self.assertGreaterEqual(hs_removed, 0)
        self.assertGreater(hs_sweep, 0)

    def test_rollback_to_stable_prepare(self):
        nrows = 1000

        # Create a table without logging.
        uri_1 = "table:rollback_to_stable10_1"
        ds_1 = SimpleDataSet(
            self, uri_1, 0, key_format="i", value_format="S", config='log=(enabled=false)')
        ds_1.populate()

        # Create another table without logging.
        uri_2 = "table:rollback_to_stable10_2"
        ds_2 = SimpleDataSet(
            self, uri_2, 0, key_format="i", value_format="S", config='log=(enabled=false)')
        ds_2.populate()

        # Pin oldest and stable to timestamp 10.
        self.conn.set_timestamp('oldest_timestamp=' + timestamp_str(10) +
            ',stable_timestamp=' + timestamp_str(10))

        value_a = "aaaaa" * 100
        value_b = "bbbbb" * 100
        value_c = "ccccc" * 100
        value_d = "ddddd" * 100
        value_e = "eeeee" * 100
        value_f = "fffff" * 100

        # Perform several updates.
        self.large_updates(uri_1, value_d, ds_1, nrows, 20)
        self.large_updates(uri_1, value_c, ds_1, nrows, 30)
        self.large_updates(uri_1, value_b, ds_1, nrows, 40)
        self.large_updates(uri_1, value_a, ds_1, nrows, 50)

        self.large_updates(uri_2, value_d, ds_2, nrows, 20)
        self.large_updates(uri_2, value_c, ds_2, nrows, 30)
        self.large_updates(uri_2, value_b, ds_2, nrows, 40)
        self.large_updates(uri_2, value_a, ds_2, nrows, 50)

        # Verify data is visible and correct.
        self.check(value_d, uri_1, nrows, 20)
        self.check(value_c, uri_1, nrows, 30)
        self.session.breakpoint()
        self.check(value_b, uri_1, nrows, 40)
        self.check(value_a, uri_1, nrows, 50)

        self.check(value_d, uri_2, nrows, 20)
        self.check(value_c, uri_2, nrows, 30)
        self.session.breakpoint()
        self.check(value_b, uri_2, nrows, 40)
        self.check(value_a, uri_2, nrows, 50)

        # Pin stable to timestamp 60 if prepare otherwise 50.
        if self.prepare:
            self.conn.set_timestamp('stable_timestamp=' + timestamp_str(60))
        else:
            self.conn.set_timestamp('stable_timestamp=' + timestamp_str(50))

        # Create a checkpoint thread
        done = threading.Event()
        ckpt = checkpoint_thread(self.conn, done)
        try:
            ckpt.start()

            # Perform several updates in parallel with checkpoint.
            session_p1 = self.conn.open_session()
            cursor_p1 = session_p1.open_cursor(uri_1)
            session_p1.begin_transaction('isolation=snapshot')
            for i in range(1, nrows):
                cursor_p1.set_key(ds_1.key(i))
                cursor_p1.set_value(value_e)
                self.assertEquals(cursor_p1.update(), 0)
            session_p1.prepare_transaction('prepare_timestamp=' + timestamp_str(69))

            # Perform several updates in parallel with checkpoint.
            session_p2 = self.conn.open_session()
            cursor_p2 = session_p2.open_cursor(uri_2)
            session_p2.begin_transaction('isolation=snapshot')
            for i in range(1, nrows):
                cursor_p2.set_key(ds_2.key(i))
                cursor_p2.set_value(value_e)
                self.assertEquals(cursor_p2.update(), 0)
            session_p2.prepare_transaction('prepare_timestamp=' + timestamp_str(69))
        finally:
            done.set()
            ckpt.join()

        # Simulate a crash by copying to a new directory(RESTART).
        copy_wiredtiger_home(".", "RESTART")

        # Commit the prepared transaction.
        session_p1.commit_transaction('commit_timestamp=' + timestamp_str(70) + ',durable_timestamp=' + timestamp_str(71))
        session_p2.commit_transaction('commit_timestamp=' + timestamp_str(70) + ',durable_timestamp=' + timestamp_str(71))
        session_p1.close()
        session_p2.close()

        # Open the new directory.
        self.conn = self.setUpConnectionOpen("RESTART")
        self.session = self.setUpSessionOpen(self.conn)

        # Check that the correct data is seen at and after the stable timestamp.
        self.check(value_a, uri_1, nrows, 50)
        self.check(value_a, uri_1, nrows, 80)
        self.check(value_b, uri_1, nrows, 40)
        self.check(value_c, uri_1, nrows, 30)
        self.check(value_d, uri_1, nrows, 20)

        # Check that the correct data is seen at and after the stable timestamp.
        self.check(value_a, uri_2, nrows, 50)
        self.check(value_a, uri_2, nrows, 80)
        self.check(value_b, uri_2, nrows, 40)
        self.check(value_c, uri_2, nrows, 30)
        self.check(value_d, uri_2, nrows, 20)

        stat_cursor = self.session.open_cursor('statistics:', None, None)
        calls = stat_cursor[stat.conn.txn_rts][2]
        hs_removed = stat_cursor[stat.conn.txn_rts_hs_removed][2]
        hs_sweep = stat_cursor[stat.conn.txn_rts_sweep_hs_keys][2]
        keys_removed = stat_cursor[stat.conn.txn_rts_keys_removed][2]
        keys_restored = stat_cursor[stat.conn.txn_rts_keys_restored][2]
        pages_visited = stat_cursor[stat.conn.txn_rts_pages_visited][2]
        upd_aborted = stat_cursor[stat.conn.txn_rts_upd_aborted][2]
        stat_cursor.close()

        self.assertEqual(calls, 0)
        self.assertEqual(keys_removed, 0)
        self.assertEqual(keys_restored, 0)
        self.assertGreaterEqual(upd_aborted, 0)
        self.assertGreater(pages_visited, 0)
        self.assertGreaterEqual(hs_removed, 0)
        self.assertGreater(hs_sweep, 0)

if __name__ == '__main__':
    wttest.run()
