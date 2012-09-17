/*-
 * Copyright (c) 2008-2012 WiredTiger, Inc.
 *	All rights reserved.
 *
 * See the file LICENSE for redistribution information.
 */

#include "wt_internal.h"

/*
 * __wt_txn_checkpoint --
 *	Checkpoint a database or a list of objects in the database.
 */
int
__wt_txn_checkpoint(WT_SESSION_IMPL *session, const char *cfg[])
{
	WT_CONNECTION_IMPL *conn;
	WT_BTREE *btree, *saved_btree;
	WT_CONFIG targetconf;
	WT_CONFIG_ITEM cval, k, v;
	WT_DECL_ITEM(tmp);
	WT_DECL_RET;
	WT_SESSION *wt_session;
	WT_TXN *txn;
	void *saved_meta_next;
	int ckpt_closed, target_list, tracking;

	conn = S2C(session);
	target_list = tracking = 0;
	txn = &session->txn;

	/* Only one checkpoint can be active at a time. */
	WT_ASSERT(session, F_ISSET(session, WT_SESSION_SCHEMA_LOCKED));

	/*
	 * Checkpoints require a snapshot to write a transactionally consistent
	 * snapshot of the data.
	 *
	 * We can't use an application's transaction: if it has uncommitted
	 * changes, they will be written in the checkpoint and may appear after
	 * a crash.
	 *
	 * Use a real snapshot transaction: we don't want any chance of the
	 * snapshot being updated during the checkpoint.  Eviction is prevented
	 * from evicting anything newer than this because we track the oldest
	 * transaction ID in the system that is not visible to all readers.
	 */
	if (F_ISSET(txn, TXN_RUNNING))
		WT_RET_MSG(session, EINVAL,
		    "Checkpoint not permitted in a transaction");

	wt_session = &session->iface;
	WT_RET(wt_session->begin_transaction(wt_session, "isolation=snapshot"));

	WT_ERR(__wt_meta_track_on(session));
	tracking = 1;

	/* Step through the list of targets and checkpoint each one. */
	WT_ERR(__wt_config_gets(session, cfg, "target", &cval));
	WT_ERR(__wt_config_subinit(session, &targetconf, &cval));
	while ((ret = __wt_config_next(&targetconf, &k, &v)) == 0) {
		if (!target_list) {
			WT_ERR(__wt_scr_alloc(session, 512, &tmp));
			target_list = 1;
		}

		if (v.len != 0)
			WT_ERR_MSG(session, EINVAL,
			    "invalid checkpoint target \"%s\": URIs may "
			    "require quoting",
			    (const char *)tmp->data);

		WT_ERR(__wt_buf_fmt(session, tmp, "%.*s", (int)k.len, k.str));
		if ((ret = __wt_schema_worker(
		    session, tmp->data, __wt_checkpoint, cfg, 0)) != 0)
			WT_ERR_MSG(session, ret, "%s", (const char *)tmp->data);
	}
	WT_ERR_NOTFOUND_OK(ret);

	if (!target_list) {
		/*
		 * Possible checkpoint name.  If checkpoints are named or we're
		 * dropping checkpoints, checkpoint both open and closed files;
		 * else, we only checkpoint open files.
		 *
		 * XXX
		 * We don't optimize unnamed checkpoints of a list of targets,
		 * we open the targets and checkpoint them even if they are
		 * quiescent and don't need a checkpoint, believing applications
		 * unlikely to checkpoint a list of closed targets.
		 */
		cval.len = 0;
		ckpt_closed = 0;
		WT_ERR(__wt_config_gets(session, cfg, "name", &cval));
		if (cval.len != 0)
			ckpt_closed = 1;
		WT_ERR(__wt_config_gets(session, cfg, "drop", &cval));
		if (cval.len != 0)
			ckpt_closed = 1;
		WT_ERR(ckpt_closed ?
		    __wt_meta_btree_apply(session, __wt_checkpoint, cfg, 0) :
		    __wt_conn_btree_apply(session, __wt_checkpoint, cfg));
	}

	/* Checkpoint the metadata file. */
	TAILQ_FOREACH(btree, &conn->btqh, q)
		if (strcmp(btree->name, WT_METADATA_URI) == 0)
			break;
	if (btree == NULL)
		WT_ERR_MSG(session, EINVAL,
		    "checkpoint unable to find open meta-data handle");

	/*
	 * Disable metadata tracking during the metadata checkpoint.
	 *
	 * We don't lock old checkpoints in the metadata file: there is no way
	 * to open one.  We are holding other handle locks, it is not safe to
	 * lock conn->spinlock.
	 */
	txn->isolation = TXN_ISO_READ_UNCOMMITTED;
	saved_meta_next = session->meta_track_next;
	session->meta_track_next = NULL;
	saved_btree = session->btree;
	session->btree = btree;
	ret = __wt_checkpoint(session, cfg);
	session->btree = saved_btree;
	session->meta_track_next = saved_meta_next;
	WT_ERR(ret);

err:	/*
	 * XXX
	 * Rolling back the changes here is problematic.
	 *
	 * If we unroll here, we need a way to roll back changes to the avail
	 * list for each tree that was successfully synced before the error
	 * occurred.  Otherwise, the next time we try this operation, we will
	 * try to free an old checkpoint again.
	 *
	 * OTOH, if we commit the changes after a failure, we have partially
	 * overwritten the checkpoint, so what ends up on disk is not
	 * consistent.
	 */
	txn->isolation = TXN_ISO_READ_UNCOMMITTED;
	if (tracking)
		WT_TRET(__wt_meta_track_off(session, ret != 0));

	__wt_txn_release(session);
	__wt_scr_free(&tmp);
	return (ret);
}

/*
 * __ckpt_name_ok --
 *	Complain if our reserved checkpoint name is used.
 */
static int
__ckpt_name_ok(WT_SESSION_IMPL *session, const char *name, size_t len)
{
	/*
	 * The internal checkpoint name is special, applications aren't allowed
	 * to use it.  Be aggressive and disallow any matching prefix, it makes
	 * things easier when checking in other places.
	 */
	if (len < strlen(WT_CHECKPOINT))
		return (0);
	if (strncmp(name, WT_CHECKPOINT, strlen(WT_CHECKPOINT)) != 0)
		return (0);

	WT_RET_MSG(session, EINVAL,
	    "the checkpoint name \"%s\" is reserved", WT_CHECKPOINT);
}

/*
 * __drop --
 *	Drop all checkpoints with a specific name.
 */
static void
__drop(WT_CKPT *ckptbase, const char *name, size_t len)
{
	WT_CKPT *ckpt;

	/*
	 * If we're dropping internal checkpoints, match to the '.' separating
	 * the checkpoint name from the generational number, and take all that
	 * we can find.  Applications aren't allowed to use any variant of this
	 * name, so the test is still pretty simple, if the leading bytes match,
	 * it's one we want to drop.
	 */
	if (strncmp(WT_CHECKPOINT, name, len) == 0) {
		WT_CKPT_FOREACH(ckptbase, ckpt)
			if (strncmp(ckpt->name,
			    WT_CHECKPOINT, strlen(WT_CHECKPOINT)) == 0)
				F_SET(ckpt, WT_CKPT_DELETE);
	} else
		WT_CKPT_FOREACH(ckptbase, ckpt)
			if (WT_STRING_MATCH(ckpt->name, name, len))
				F_SET(ckpt, WT_CKPT_DELETE);
}

/*
 * __drop_from --
 *	Drop all checkpoints after, and including, the named checkpoint.
 */
static void
__drop_from(WT_CKPT *ckptbase, const char *name, size_t len)
{
	WT_CKPT *ckpt;
	int matched;

	/*
	 * There's a special case -- if the name is "all", then we delete all
	 * of the checkpoints.
	 */
	if (WT_STRING_MATCH("all", name, len)) {
		WT_CKPT_FOREACH(ckptbase, ckpt)
			F_SET(ckpt, WT_CKPT_DELETE);
		return;
	}

	/*
	 * We use the first checkpoint we can find, that is, if there are two
	 * checkpoints with the same name in the list, we'll delete from the
	 * first match to the end.
	 */
	matched = 0;
	WT_CKPT_FOREACH(ckptbase, ckpt) {
		if (!matched && !WT_STRING_MATCH(ckpt->name, name, len))
			continue;

		matched = 1;
		F_SET(ckpt, WT_CKPT_DELETE);
	}
}

/*
 * __drop_to --
 *	Drop all checkpoints before, and including, the named checkpoint.
 */
static void
__drop_to(WT_CKPT *ckptbase, const char *name, size_t len)
{
	WT_CKPT *ckpt, *mark;

	/*
	 * We use the last checkpoint we can find, that is, if there are two
	 * checkpoints with the same name in the list, we'll delete from the
	 * beginning to the second match, not the first.
	 */
	mark = NULL;
	WT_CKPT_FOREACH(ckptbase, ckpt)
		if (WT_STRING_MATCH(ckpt->name, name, len))
			mark = ckpt;

	if (mark == NULL)
		return;

	WT_CKPT_FOREACH(ckptbase, ckpt) {
		F_SET(ckpt, WT_CKPT_DELETE);

		if (ckpt == mark)
			break;
	}
}

/*
 * __wt_checkpoint --
 *	Checkpoint a tree.
 */
int
__wt_checkpoint(WT_SESSION_IMPL *session, const char *cfg[])
{
	WT_BTREE *btree;
	WT_CKPT *ckpt, *ckptbase;
	WT_CONFIG dropconf;
	WT_CONFIG_ITEM cval, k, v;
	WT_CONNECTION_IMPL *conn;
	WT_DECL_RET;
	WT_TXN *txn;
	WT_TXN_ISOLATION saved_isolation;
	const char *name;
	char *name_alloc;
	int deleted, is_checkpoint;

	conn = S2C(session);
	btree = session->btree;
	ckpt = ckptbase = NULL;
	name_alloc = NULL;
	txn = &session->txn;
	saved_isolation = txn->isolation;

	/*
	 * We're called in two ways: either because a handle is closing or
	 * session.checkpoint was called, figure it out.
	 */
	is_checkpoint = cfg == NULL ? 0 : 1;

	/*
	 * Checkpoint handles are read-only by definition and don't participate
	 * in checkpoints.   Closing one discards its blocks, otherwise there's
	 * no work to do.
	 */
	if (btree->checkpoint != NULL)
		return (is_checkpoint ? 0 :
		    __wt_bt_cache_flush(
		    session, NULL, WT_SYNC_DISCARD_NOWRITE));

	/*
	 * If closing a file that's never been modified, discard its blocks.
	 * If checkpoint of a file that's never been modified, we may still
	 * have to checkpoint it, we'll test again once we understand the
	 * nature of the checkpoint.
	 */
	if (!btree->modified && !is_checkpoint)
		return (__wt_bt_cache_flush(
		    session, NULL, WT_SYNC_DISCARD_NOWRITE));

	/*
	 * Get the list of checkpoints for this file.  If there's no reference
	 * to the file in the metadata (the file is dead), then discard it from
	 * the cache without bothering to write any dirty pages.
	 */
	if ((ret = __wt_meta_ckptlist_get(
	    session, btree->name, &ckptbase)) == WT_NOTFOUND)
		return (__wt_bt_cache_flush(
		    session, NULL, WT_SYNC_DISCARD_NOWRITE));
	WT_ERR(ret);

	/* This may be a named checkpoint, check the configuration. */
	cval.len = 0;
	if (cfg != NULL)
		WT_ERR(__wt_config_gets(session, cfg, "name", &cval));
	if (cval.len == 0)
		name = WT_CHECKPOINT;
	else {
		WT_ERR(__ckpt_name_ok(session, cval.str, cval.len));
		WT_ERR(__wt_strndup(session, cval.str, cval.len, &name_alloc));
		name = name_alloc;
	}

	/* We may be dropping specific checkpoints, check the configuration. */
	if (cfg != NULL) {
		cval.len = 0;
		WT_ERR(__wt_config_gets(session, cfg, "drop", &cval));
		if (cval.len != 0) {
			WT_ERR(__wt_config_subinit(session, &dropconf, &cval));
			while ((ret =
			    __wt_config_next(&dropconf, &k, &v)) == 0) {
				/* Disallow the reserved checkpoint name. */
				if (v.len == 0)
					WT_ERR(__ckpt_name_ok(
					    session, k.str, k.len));
				else
					WT_ERR(__ckpt_name_ok(
					    session, v.str, v.len));

				if (v.len == 0)
					__drop(ckptbase, k.str, k.len);
				else if (WT_STRING_MATCH("from", k.str, k.len))
					__drop_from(ckptbase, v.str, v.len);
				else if (WT_STRING_MATCH("to", k.str, k.len))
					__drop_to(ckptbase, v.str, v.len);
				else
					WT_ERR_MSG(session, EINVAL,
					    "unexpected value for checkpoint "
					    "key: %.*s",
					    (int)k.len, k.str);
			}
			WT_ERR_NOTFOUND_OK(ret);
		}
	}

	/* Drop checkpoints with the same name as the one we're taking. */
	__drop(ckptbase, name, strlen(name));

	/*
	 * Check for clean objects not requiring a checkpoint.
	 *
	 * If we're closing a handle, and the object is clean, we can skip the
	 * checkpoint, whatever checkpoints we have are sufficient.  (We might
	 * not have any checkpoints if the object was never modified, and that's
	 * OK: the object creation code doesn't mark the tree modified so we can
	 * skip newly created trees here.)
	 *
	 * If the application repeatedly checkpoints an object (imagine hourly
	 * checkpoints using the same explicit or internal name), there's no
	 * reason to repeat the checkpoint for clean objects.  The test is if
	 * the only checkpoint we're deleting is the last one in the list and
	 * it has the same name as the checkpoint we're about to take, skip the
	 * work.  (We can skip checkpoints that delete more than the last
	 * checkpoint because deleting those checkpoints might free up space in
	 * the file.)  This means an application toggling between two (or more)
	 * checkpoint names will repeatedly take empty checkpoints, but that's
	 * not likely enough to make detection worthwhile.
	 *
	 * Checkpoint read-only objects otherwise: the application must be able
	 * to open the checkpoint in a cursor after taking any checkpoint, which
	 * means it must exist.
	 */
	if (!btree->modified) {
		if (!is_checkpoint)
			goto skip;

		deleted = 0;
		WT_CKPT_FOREACH(ckptbase, ckpt)
			if (F_ISSET(ckpt, WT_CKPT_DELETE))
				++deleted;
		if (deleted == 1 &&
		    F_ISSET(ckpt - 1, WT_CKPT_DELETE) &&
		    strcmp(name, (ckpt - 1)->name) == 0)
			goto skip;
	}

	/* Add a new checkpoint entry at the end of the list. */
	WT_CKPT_FOREACH(ckptbase, ckpt)
		;
	WT_ERR(__wt_strdup(session, name, &ckpt->name));
	F_SET(ckpt, WT_CKPT_ADD);

	/*
	 * Lock the checkpoints that will be deleted.
	 *
	 * Checkpoints are only locked when tracking is enabled, which covers
	 * sync and drop operations, but not close.  The reasoning is that
	 * there should be no access to a checkpoint during close, because any
	 * thread accessing a checkpoint will also have the current file handle
	 * open.
	 */
	if (WT_META_TRACKING(session))
		WT_CKPT_FOREACH(ckptbase, ckpt) {
			if (!F_ISSET(ckpt, WT_CKPT_DELETE))
				continue;

			/*
			 * We can't drop/update checkpoints if a backup cursor
			 * is open.  WiredTiger checkpoints are uniquely named
			 * and it's OK to have multiple in the system: clear the
			 * delete flag, and otherwise fail.
			 */
			if (conn->ckpt_backup) {
				if (strncmp(ckpt->name,
				    WT_CHECKPOINT,
				    strlen(WT_CHECKPOINT)) == 0) {
					F_CLR(ckpt, WT_CKPT_DELETE);
					continue;
				}
				WT_ERR_MSG(session, EBUSY,
				    "checkpoints cannot be dropped when "
				    "backup cursors are open");
			}

			/*
			 * We can't drop/update checkpoints if referenced by a
			 * cursor.  WiredTiger checkpoints are uniquely named
			 * and it's OK to have multiple in the system: clear the
			 * delete flag, and otherwise fail.
			 */
			ret =
			    __wt_session_lock_checkpoint(session, ckpt->name);
			if (ret == 0)
				continue;
			if (ret == EBUSY && strncmp(ckpt->name,
			    WT_CHECKPOINT, strlen(WT_CHECKPOINT)) == 0) {
				F_CLR(ckpt, WT_CKPT_DELETE);
				continue;
			}
			WT_ERR_MSG(session, ret,
			    "checkpoints cannot be dropped when in-use");
		}

	/*
	 * Mark the root page dirty to ensure something gets written.
	 *
	 * Don't test the tree modify flag first: if the tree is modified,
	 * we must write the root page anyway, we're not adding additional
	 * writes to the process.   If the tree is not modified, we have to
	 * dirty the root page to ensure something gets written.  This is
	 * really about paranoia: if the tree modification value gets out of
	 * sync with the set of dirty pages (modify is set, but there are no
	 * dirty pages), we do a checkpoint without any writes, no checkpoint
	 * is created, and then things get bad.
	 */
	WT_ERR(__wt_bt_cache_force_write(session));

	/*
	 * Clear the tree's modified flag; any changes before we clear the flag
	 * are guaranteed to be part of this checkpoint (unless reconciliation
	 * skips updates for transactional reasons), and changes subsequent to
	 * the checkpoint start, which might not be included, will re-set the
	 * modified flag.  The "unless reconciliation skips updates" problem is
	 * handled in the reconciliation code: if reconciliation skips updates,
	 * it sets the modified flag itself.  Use a full barrier so we get the
	 * store done quickly, this isn't a performance path.
	 */
	btree->modified = 0;
	WT_FULL_BARRIER();

	/* If closing a handle, include everything in the checkpoint. */
	if (!is_checkpoint)
		txn->isolation = TXN_ISO_READ_UNCOMMITTED;

	/* Flush the file from the cache, creating the checkpoint. */
	WT_ERR(__wt_bt_cache_flush(session,
	    ckptbase, is_checkpoint ? WT_SYNC : WT_SYNC_DISCARD));

	/* Update the object's metadata. */
	txn = &session->txn;
	txn->isolation = TXN_ISO_READ_UNCOMMITTED;
	ret = __wt_meta_ckptlist_set(session, btree->name, ckptbase);
	WT_ERR(ret);

	/*
	 * If tracking enabled, defer making pages available until transaction
	 * end.  The exception is if the handle is being discarded, in which
	 * case the handle will be gone by the time we try to apply or unroll
	 * the meta tracking event.
	 */
	if (WT_META_TRACKING(session) && is_checkpoint)
		WT_ERR(__wt_meta_track_checkpoint(session));
	else
		WT_ERR(__wt_bm_checkpoint_resolve(session));

err:
skip:	__wt_meta_ckptlist_free(session, ckptbase);
	__wt_free(session, name_alloc);
	txn->isolation = saved_isolation;

	return (ret);
}
