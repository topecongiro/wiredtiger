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
#
# test_txn22.py
#   Transactions and eviction: Test if using snapshot isolation for eviction threads helps with
#   cache stuck issue.
#

import wiredtiger, wttest
import time

class test_txn22(wttest.WiredTigerTestCase):

    session_config = 'isolation=snapshot'

    def conn_config(self):
        # We want to either eliminate or keep the application thread role in eviction to minimum.
        # This will ensure that the dedicated eviction threads are doing the heavy lifting.
        return 'cache_size=100MB,eviction_target=80,eviction_dirty_target=5,eviction_trigger=100,\
                eviction_updates_target=5,eviction_dirty_trigger=99,eviction_updates_trigger=100,\
                eviction=(threads_max=4)'

    def test_snapshot_isolation_and_eviction(self):

        # Create and populate a table.
        uri = "table:test_txn22"
        table_params = 'key_format=i,value_format=S'
        default_val = 'ABCD' * 60
        new_val = 'YYYY' * 60
        final_val = 'ZZZZ' * 60
        n_rows = 480000

        self.session.create(uri, table_params)
        cursor = self.session.open_cursor(uri, None)
        for i in range(0, n_rows):
            cursor[i] = default_val
        cursor.close()

        # Perform a checkpoint. There should be no dirty content in the cache after this.
        self.session.checkpoint()

        # Start a transaction, make an update and keep it running.
        cursor = self.session.open_cursor(uri, None)
        self.session.begin_transaction('isolation=snapshot')
        cursor[0] = new_val

        # Start few sessions and transactions, make updates and try committing them.
        session2 = self.setUpSessionOpen(self.conn)
        cursor2 = session2.open_cursor(uri)
        session2.begin_transaction('isolation=snapshot')
        start_row = int(n_rows/4)
        for i in range(0, 130000):
            cursor2[start_row + i] = new_val
        session2.commit_transaction()

        session3 = self.setUpSessionOpen(self.conn)
        cursor3 = session3.open_cursor(uri)
        session3.begin_transaction('isolation=snapshot')
        start_row = int(n_rows/2)
        for i in range(0, 130000):
            cursor3[start_row + i] = new_val
        session3.commit_transaction()

        # At this point in time, we have made roughly 90% cache dirty. If we are not using
        # snaphsots for eviction threads, the cache state will remain like this forever and we may
        # never reach this part of code. We might get a rollback error by now or WT will panic with
        # cache stuck error.
        #
        # Even if we dont get an error by now and if we try to insert new data at this point in
        # time, dirty cache usage will exceed 100% if eviction threads are not using snapshot
        # isolation. In that case, we will eventually get a rollback error for sure.

        session4 = self.setUpSessionOpen(self.conn)
        cursor4 = session4.open_cursor(uri)
        session4.begin_transaction('isolation=snapshot')
        for i in range(1, 100000):
            cursor4[i] = final_val
        session4.commit_transaction()

        # If we have done all operations error free so far, eviction threads have been successful.

        self.session.commit_transaction()
        cursor.close()
        self.session.close()

        cursor2.close()
        session2.close()

        cursor3.close()
        session3.close()

        cursor4.close()
        session4.close()

if __name__ == '__main__':
    wttest.run()
