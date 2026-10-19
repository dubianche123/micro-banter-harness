"""Offline state/summary regressions. No provider or QQ calls."""
import copy
import json
import os
import sys
from pathlib import Path
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from archive import MessageArchive
from digest import GroupDigest, PromiseBook, compress_group
from relations import RelationStore


class RelationPersistenceTest(unittest.TestCase):
    def test_rank_settles_each_idle_interval_once_and_survives_restart(self):
        now = time.time()
        store = RelationStore()
        rec = store.get("g", "u")
        rec.update(score=50, interactions=3, last_seen=now - 6 * 86400)
        self.assertEqual([store.rank("g", now=now)[0][1]["score"] for _ in range(3)],
                         [48, 48, 48])
        data = {}
        store.dump_into(data)
        restored = RelationStore()
        restored.hydrate(copy.deepcopy(data["relations"]))
        self.assertEqual(restored.rank("g", now=now)[0][1]["score"], 48)
        self.assertEqual(restored.rank("g", now=now + 2 * 86400)[0][1]["score"], 47)

    def test_mode_and_told_day_and_extension_fields_round_trip(self):
        store = RelationStore()
        rec = store.get("g", "u")
        rec.update(mode="知心", told_day="2026-10-19", future_field={"value": 1})
        saved = {}
        store.dump_into(saved)
        restored = RelationStore()
        restored.hydrate(copy.deepcopy(saved["relations"]))
        self.assertEqual(restored.get("g", "u"), rec | {"decay_steps": 0})

    def test_new_interaction_restarts_the_idle_clock(self):
        now = time.time()
        store = RelationStore()
        rec = store.get("g", "u")
        rec.update(score=-50, interactions=3, last_seen=now - 6 * 86400)
        store.rank("g", now=now)
        store.apply("g", "u", 0, now=now)
        self.assertEqual(rec["score"], -48)
        self.assertEqual(rec["decay_steps"], 0)
        self.assertEqual(store.rank("g", now=now + 86400)[0][1]["score"], -48)


    def test_hydrate_discards_stale_owner_flag_but_keeps_persistent_fields(self):
        now = time.time()
        store = RelationStore()
        store.hydrate({"g|former-owner": {
            "score": 100, "pinned": True, "last_seen": now,
            "mode": "知心", "told_day": "2026-10-19", "extra": {"kept": True}}})
        rec = store.get("g", "former-owner")
        self.assertNotIn("pinned", rec)
        store.apply("g", "former-owner", -3, now=now, count=False)
        self.assertEqual(rec["score"], 97)
        self.assertEqual((rec["mode"], rec["told_day"], rec["extra"]),
                         ("知心", "2026-10-19", {"kept": True}))

    def test_current_pin_is_applied_before_or_after_hydrate_and_unpin_is_effective(self):
        now = time.time()
        saved = {"g|owner": {"score": 50, "last_seen": now - 6 * 86400,
                              "interactions": 3}}
        for pin_before in (False, True):
            with self.subTest(pin_before=pin_before):
                store = RelationStore()
                if pin_before:
                    store.pin("owner")
                store.hydrate(saved)
                if not pin_before:
                    store.pin("owner")
                self.assertEqual(store.rank("g", now=now)[0][1]["score"], 100)
                store.unpin("owner")
                self.assertEqual(store.rank("g", now=now)[0][1]["score"], 98)
                self.assertNotIn("pinned", store.get("g", "owner"))


class ArchiveCursorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.archive = MessageArchive(self.tmp.name, keep_days=0)

    def tearDown(self):
        self.archive.close()
        self.tmp.cleanup()

    def test_equal_timestamp_and_backdated_arrivals_stay_pending(self):
        now = time.time()
        self.archive.append("g", "a", "first", ts=now)
        first, cursor = self.archive.pending("g")
        self.archive.append("g", "b", "same timestamp", ts=now)
        self.archive.append("g", "b", "late earlier day", ts=now - 86400)
        pending, _ = self.archive.pending("g", cursor=cursor, since_ts=now + 100)
        self.assertEqual({r["text"] for r in pending}, {"same timestamp", "late earlier day"})
        self.assertEqual(len(first), 1)

    def test_limit_reads_oldest_prefix_and_round_trips_cursor(self):
        for i in range(3):
            self.archive.append("g", "u", f"message {i}")
        first, cursor = self.archive.pending("g", limit=1)
        self.assertEqual([r["text"] for r in first], ["message 0"])
        store = GroupDigest()
        store.mark_compressed("g", cursor=cursor)
        saved = {}
        store.dump_into(saved)
        restored = GroupDigest()
        restored.hydrate(json.loads(json.dumps(saved["digests"])))
        after, _ = self.archive.pending("g", cursor=restored.cursor("g"))
        self.assertEqual([r["text"] for r in after], ["message 1", "message 2"])

    def test_timestamp_only_state_migrates_once(self):
        now = time.time()
        self.archive.append("g", "u", "old", ts=now - 1)
        self.archive.append("g", "u", "new", ts=now + 1)
        rows, cursor = self.archive.pending("g", since_ts=now)
        self.assertEqual([r["text"] for r in rows], ["new"])
        self.archive.append("g", "u", "after migration", ts=now - 2)
        rows, _ = self.archive.pending("g", cursor=cursor, since_ts=now)
        self.assertEqual([r["text"] for r in rows], ["after migration"])

    def test_legacy_filtered_tail_is_not_replayed_after_prefix_commit(self):
        now = time.time()
        self.archive.append("g", "u", "new first", ts=now + 1)
        self.archive.append("g", "u", "old tail", ts=now - 1)
        rows, _ = self.archive.pending("g", since_ts=now)
        self.assertEqual([row["text"] for row in rows], ["new first"])
        pending, _ = self.archive.pending("g", cursor=rows[-1]["_cursor"])
        self.assertEqual(pending, [])

    def test_legacy_limit_retains_frozen_bound_without_dropping_late_old_timestamp(self):
        now = time.time()
        self.archive.append("g", "u", "new first", ts=now + 1)
        self.archive.append("g", "u", "old tail", ts=now - 1)
        rows, _ = self.archive.pending("g", since_ts=now, limit=1)
        cursor = json.loads(json.dumps(rows[-1]["_cursor"]))
        self.archive.append("g", "u", "late old timestamp", ts=now - 2)
        pending, cursor = self.archive.pending("g", cursor=cursor)
        self.assertEqual([row["text"] for row in pending], ["late old timestamp"])
        self.assertNotIn("_legacy", cursor)
        self.assertEqual(self.archive.pending("g", cursor=cursor)[0], [])

    def test_command_skip_still_advances_snapshot(self):
        self.archive.append("g", "u", "command", cmd=True)
        rows, cursor = self.archive.pending("g")
        self.assertEqual(rows, [])
        self.assertTrue(cursor)
        self.archive.append("g", "u", "chat")
        rows, _ = self.archive.pending("g", cursor=cursor)
        self.assertEqual([r["text"] for r in rows], ["chat"])

    def test_caption_is_persisted_source_linked_and_late_evidence_is_pending(self):
        a = self.archive.append("g", "a", "[图片]", message_id="image-a")
        self.archive.append("g", "b", "[图片]", message_id="image-b")
        _, cursor = self.archive.pending("g")
        self.archive.close()
        with open(self.archive._file_for(a["day"]), encoding="utf-8") as fp:
            original = fp.readline()
        restored = MessageArchive(self.tmp.name, keep_days=0)
        try:
            self.assertIsNone(restored.append_caption("g", "image-b", "cat", sender="a"))
            self.assertIsNone(restored.append_caption("other", "image-b", "cat"))
            caption = restored.append_caption("g", "image-b", "B 的猫", sender="b")
            self.assertEqual(caption["message_id"], "image-b")
            self.assertEqual(restored.append_caption("g", "image-b", "B 的猫"), caption)
            rows, _ = restored.pending("g", cursor=cursor)
            self.assertEqual([(r["sender"], r["text"]) for r in rows], [("b", "[图片：B 的猫]")])
            with open(restored._file_for(a["day"]), encoding="utf-8") as fp:
                self.assertEqual(fp.readline(), original)
        finally:
            restored.close()
        restarted = MessageArchive(self.tmp.name, keep_days=0)
        try:
            self.assertEqual(restarted.append_caption("g", "image-b", "B 的猫"), caption)
            rows, _ = restarted.pending("g", cursor=cursor)
            self.assertEqual(len(rows), 1, "a restart must not append the same evidence twice")
        finally:
            restarted.close()

    def test_partial_tail_is_not_consumed(self):
        day = time.strftime("%Y-%m-%d")
        path = self.archive._file_for(day)
        row = {"group": "g", "sender": "u", "text": "partial", "ts": time.time()}
        raw = json.dumps(row).encode()
        with open(path, "wb") as fp:
            fp.write(raw[:20])
        rows, cursor = self.archive.pending("g")
        self.assertEqual(rows, [])
        with open(path, "ab") as fp:
            fp.write(raw[20:] + b"\n")
        rows, _ = self.archive.pending("g", cursor=cursor)
        self.assertEqual(rows[0]["text"], "partial")


class DigestAtomicityTest(unittest.IsolatedAsyncioTestCase):
    async def test_one_map_failure_does_not_commit_or_reduce(self):
        store = GroupDigest(chunk_chars=8)
        old = {"brief": "old", "data": {"people": []}}
        store.touch("g")["summary"] = old
        calls = []

        async def ask(system, payload):
            calls.append(payload)
            return "map ok" if len(calls) == 1 else None

        result = await compress_group(store, "g", [
            {"sender": "u", "text": "first message"},
            {"sender": "u", "text": "second message"}], ask)
        self.assertIsNone(result)
        self.assertIs(store.get("g"), old)
        self.assertEqual(len(calls), 2)

    async def test_refusal_or_malformed_structure_keeps_old_summary(self):
        invalid = ["sorry", "text<DIGEST>{}</DIGEST>",
                   'text<DIGEST>{"people":"invalid"}</DIGEST>',
                   'text<DIGEST>{"people":[{}]}</DIGEST>',
                   'text<DIGEST>{"people":[null]}</DIGEST>']
        for raw in invalid:
            with self.subTest(raw=raw):
                store = GroupDigest()
                old = {"brief": "old", "data": {"people": []}}
                store.touch("g")["summary"] = old

                async def ask(system, payload):
                    return raw

                result = await compress_group(store, "g", [{"sender": "u", "text": "new"}], ask)
                self.assertIsNone(result)
                self.assertIs(store.get("g"), old)

    async def test_valid_empty_structure_can_clear_summary(self):
        async def ask(system, payload):
            return 'Quiet<DIGEST>{"people":[],"promises":[]}</DIGEST>'
        self.assertIsNotNone(await compress_group(GroupDigest(), "g", [
            {"sender": "u", "text": "hello"}], ask))

    async def test_max_blocks_commits_only_processed_prefix_without_cursor_in_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = MessageArchive(tmp, keep_days=0)
            try:
                for i in range(3):
                    archive.append("g", "u", f"message {i}")
                entries, _ = archive.pending("g")
                seen = []

                async def ask(system, payload):
                    seen.append(payload)
                    return 'Brief<DIGEST>{"people":[]}</DIGEST>'

                store = GroupDigest(chunk_chars=8, max_blocks=1)
                result = await compress_group(store, "g", entries, ask)
                store.mark_compressed("g", cursor=result["source_cursor"])
                pending, _ = archive.pending("g", cursor=store.cursor("g"))
                self.assertEqual(result["entry_count"], 1)
                self.assertEqual([r["text"] for r in pending], ["message 1", "message 2"])
                self.assertIn("message 0", seen[0])
                self.assertNotIn("message 1", seen[0])
                self.assertNotIn("_cursor", seen[0])
                self.assertNotIn(".jsonl", seen[0])
            finally:
                archive.close()


    async def test_max_blocks_retains_legacy_bound_through_filtered_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = MessageArchive(tmp, keep_days=0)
            try:
                now = time.time()
                archive.append("g", "u", "new first", ts=now + 1)
                archive.append("g", "u", "new second", ts=now + 2)
                archive.append("g", "u", "old filtered tail", ts=now - 1)
                entries, _ = archive.pending("g", since_ts=now)
                store = GroupDigest(chunk_chars=8, max_blocks=1)

                async def ask(system, payload):
                    return 'Brief<DIGEST>{"people":[]}</DIGEST>'

                result = await compress_group(store, "g", entries, ask)
                self.assertIn("_legacy", result["source_cursor"])
                store.mark_compressed("g", cursor=result["source_cursor"])
                entries, _ = archive.pending("g", cursor=store.cursor("g"))
                self.assertEqual([row["text"] for row in entries], ["new second"])
                result = await compress_group(store, "g", entries, ask)
                store.mark_compressed("g", cursor=result["source_cursor"])
                self.assertEqual(archive.pending("g", cursor=store.cursor("g"))[0], [])
            finally:
                archive.close()


class PromiseIdentityTest(unittest.TestCase):
    def test_rename_preserves_ids_and_nag_and_legacy_data(self):
        book = PromiseBook()
        book.hydrate({"g": [{"id": "legacy-id", "who": "旧名", "what": "请奶茶",
                             "nag": 1, "last_nag": 50, "last_shown": "2026-10-19"}]})
        self.assertEqual(book.rename_member("g", "member", "新名", old="旧名"), 1)
        added, dropped = book.sync("g", [{"who": "新名", "what": "请奶茶"}],
                                   member_resolver=lambda name: "member")
        self.assertEqual((added, dropped), (0, 0))
        row = book.list("g")[0]
        self.assertEqual((row["id"], row["member_id"], row["nag"], row["last_shown"]),
                         ("legacy-id", "member", 1, "2026-10-19"))
        self.assertEqual(book.resolve("g", who="member"), 1)

    def test_same_member_with_new_label_matches_without_explicit_rename(self):
        book = PromiseBook()
        book.sync("g", [{"who": "旧名", "what": "请奶茶"}], member_resolver=lambda _: "u")
        pid = book.list("g")[0]["id"]
        book.mark_nagged("g", pid)
        book.sync("g", [{"who": "新名", "what": "请奶茶"}], member_resolver=lambda _: "u")
        self.assertEqual(book.list("g")[0]["id"], pid)
        self.assertEqual(book.list("g")[0]["nag"], 1)

    def test_finished_identity_survives_rename_and_restart(self):
        book = PromiseBook(max_nag=1)
        book.sync("g", [{"who": "旧名", "what": "请奶茶"}], member_resolver=lambda _: "u")
        book.mark_nagged("g", book.list("g")[0]["id"])
        book.rename_member("g", "u", "新名", old="旧名")
        data = {}
        book.dump_into(data)
        restored = PromiseBook()
        restored.hydrate(data["promises"], finished=data["promises_finished"])
        self.assertEqual(restored.sync("g", [{"who": "新名", "what": "请奶茶"}],
                                       member_resolver=lambda _: "u"), (0, 0))
        self.assertEqual(restored.list("g"), [])

    def test_old_truncated_promise_migrates_unique_full_text_without_reset(self):
        text = ("prepare complete experiment notes and share with everyone " * 3).strip()
        book = PromiseBook()
        book.hydrate({"g": [{"id": "old-long-id", "who": "person", "what": text[:80],
                             "nag": 1, "last_nag": 123, "last_shown": "2026-10-19"}]})
        self.assertEqual(book.sync("g", [{"who": "person", "what": text}],
                                   member_resolver=lambda _: "u"), (0, 0))
        row = book.list("g")[0]
        self.assertEqual((row["id"], row["what"], row["nag"], row["last_nag"], row["last_shown"]),
                         ("old-long-id", text, 1, 123, "2026-10-19"))
        data = {}
        book.dump_into(data)
        restored = PromiseBook()
        restored.hydrate(data["promises"])
        self.assertEqual(restored.sync("g", [{"who": "person", "what": text}],
                                       member_resolver=lambda _: "u"), (0, 0))
        self.assertEqual(restored.list("g")[0]["id"], "old-long-id")

    def test_old_truncated_promise_does_not_merge_ambiguous_completions(self):
        prefix = "x" * 80
        book = PromiseBook()
        book.hydrate({"g": [{"id": "old-long-id", "who": "person", "what": prefix, "nag": 1}]})
        book.sync("g", [{"who": "person", "what": prefix + " alpha"},
                        {"who": "person", "what": prefix + " beta"}])
        self.assertEqual(len(book.list("g")), 2)
        self.assertNotIn("old-long-id", {row["id"] for row in book.list("g")})

    def test_new_complete_80_character_promise_is_not_legacy_truncation(self):
        prefix = "x" * 80
        book = PromiseBook()
        book.sync("g", [{"who": "person", "what": prefix}])
        pid = book.list("g")[0]["id"]
        data = {}
        book.dump_into(data)
        restored = PromiseBook()
        restored.hydrate(data["promises"])
        restored.sync("g", [{"who": "person", "what": prefix + " different promise"}])
        self.assertNotEqual(restored.list("g")[0]["id"], pid)

    def test_ids_do_not_collide_on_long_shared_text_prefix(self):
        book = PromiseBook()
        text = "long promise " * 12
        book.sync("g", [{"who": "same", "what": text + "A"},
                        {"who": "same", "what": text + "B"}])
        self.assertEqual(len({row["id"] for row in book.list("g")}), 2)


if __name__ == "__main__":
    unittest.main()
