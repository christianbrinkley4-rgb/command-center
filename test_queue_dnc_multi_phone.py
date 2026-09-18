"""Regression coverage for retaining DNC leads and all their phone numbers."""
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

os.environ.update(CC_NO_AUTOSTART="1", CC_AGENT_TOKEN="", CC_PASSWORD="",
                  CC_TARGET_BIRTH_YEARS="1962", CC_GEO_FILTER_ENABLED="1",
                  CC_LOOKUP_FILTER_ENABLED="1", CC_MAX_CALL_ATTEMPTS="4")
import db

_tmp = tempfile.TemporaryDirectory()
db.DB_PATH = str(Path(_tmp.name) / "queue.db")
_original_connect = db.connect
db.connect = lambda *a, **k: _original_connect(db.DB_PATH)
db.init_db(db.DB_PATH)
import agent_api
agent_api._ensure_columns()
import app
import automation
import build_dialer_queue
import oscr_ingestion_agent as oscr
import import_lead_files
import pandas as pd

automation.ensure_schema()
oscr.ensure_schema()
client = app.app.test_client()


class QueuePhoneTests(unittest.TestCase):
    def setUp(self):
        with db.connect() as conn:
            for table in ("call_attempts", "scheduled_tasks", "suppressions", "phone_numbers",
                          "person_notes", "oscr_seen_leads", "people"):
                conn.execute(f"DELETE FROM {table}")

    def seed(self, stage="new", phone_status="active"):
        now = datetime.now().isoformat(timespec="seconds")
        with db.connect() as conn:
            pid = conn.execute("""INSERT INTO people(person_key, full_name, city, birthday,
                source, owner, stage, lead_score, first_seen_at, last_activity_at)
                VALUES ('test','Test Lead','Greensboro','1962-01-01','T65_January','chris',?,80,?,?)""",
                (stage, now, now)).lastrowid
            for phone in ("+13365551001", "+13365551002"):
                conn.execute("INSERT INTO phone_numbers(person_id,e164,status,created_at) VALUES (?,?,?,?)",
                             (pid, phone, phone_status, now))
        return pid

    def queues(self):
        api = client.get("/api/dialer-queue?owner=chris")
        agent = client.get("/agent/leads/call-queue?owner=chris")
        self.assertEqual(api.status_code, 200)
        self.assertEqual(agent.status_code, 200)
        return [api.json["queue"], app._build_ranked_queue("chris"), build_dialer_queue.build_queue("chris"),
                [{"Phone": r["phone"], "person_id": r["id"]} for r in agent.json["leads"]]]

    def assert_both(self):
        for rows in self.queues():
            self.assertEqual([r["Phone"] for r in rows], ["+13365551001", "+13365551002"])

    def test_dnc_stage_suppression_and_disposition_keep_both_numbers(self):
        pid = self.seed("dnc", "suppressed")
        with db.connect() as conn:
            conn.execute("INSERT INTO suppressions(scope,value,reason) VALUES ('person',?,'dnc')", (str(pid),))
            conn.execute("INSERT INTO suppressions(scope,value,reason) VALUES ('phone','+13365551001','oscr_dnc')")
            conn.execute("""INSERT INTO call_attempts(person_id,phone,disposition,disposition_category,dialed_at)
                VALUES (?,'+13365551001','do_not_call','dnc','2026-01-01T10:00:00')""", (pid,))
        self.assert_both()

    def test_new_lead_has_both_numbers_once(self):
        self.seed()
        self.assert_both()

    def test_retry_keeps_all_phone_rows(self):
        pid = self.seed("voicemail")
        with db.connect() as conn:
            for phone in ("+13365551001", "+13365551002"):
                conn.execute("""INSERT INTO call_attempts(person_id,phone,disposition,disposition_category,dialed_at)
                    VALUES (?,?,'no_answer','no_answer','2026-01-01T10:00:00')""", (pid, phone))
        self.assert_both()

    def test_unusable_first_phone_does_not_hide_second(self):
        self.seed()
        with db.connect() as conn:
            conn.execute("UPDATE phone_numbers SET status='bad' WHERE e164='+13365551001'")
        for rows in self.queues():
            self.assertEqual([r["Phone"] for r in rows], ["+13365551002"])

    def test_landline_first_phone_does_not_hide_second_on_dashboard(self):
        self.seed()
        with db.connect() as conn:
            conn.execute("UPDATE phone_numbers SET line_type='landline' WHERE e164='+13365551001'")
        self.assertEqual([r["Phone"] for r in app._build_ranked_queue("chris")], ["+13365551002"])

    def test_due_scheduled_lead_is_not_duplicated(self):
        pid = self.seed()
        with db.connect() as conn:
            conn.execute("""INSERT INTO scheduled_tasks(person_id,kind,due_at,status,reason)
                VALUES (?,'call','2026-01-01T10:00:00','pending','callback_requested')""", (pid,))
        self.assert_both()

    def test_api_import_retains_dnc_and_phone_arrays_on_reimport(self):
        payload = {"source": "T65_January", "owner": "chris", "leads": [{
            "name": "Test Lead", "city": "Greensboro", "birthday": "1962-01-01", "dnc": True,
            "phones": ["3365551001", "DNC: 3365551002", "336-555-1001"], "HomePhone": "DNC"}]}
        with patch("lead_scoring.score_batch"):
            first = client.post("/agent/leads/import", json=payload)
            second = client.post("/agent/leads/import", json=payload)
        self.assertEqual(first.json["imported"], 1)
        self.assertEqual(first.json["suppressed"], 0)
        self.assertEqual(second.json["duplicates"], 1)
        self.assert_both()

    def test_oscr_reimport_adds_second_number(self):
        raw = {"id": "oscr-test", "name": "Test Lead", "city": "Greensboro",
               "birthday": "1962-01-01", "phone": "3365551001", "do_not_call": True}
        cfg = oscr.Config()
        self.assertEqual(oscr.ingest_lead(oscr.normalize_api_lead(raw, cfg), cfg)[0], "imported")
        raw["home_phone"] = "3365551002"
        self.assertEqual(oscr.ingest_lead(oscr.normalize_api_lead(raw, cfg), cfg)[0], "duplicate_oscr_id")
        self.assert_both()

    def test_previously_skipped_oscr_dnc_can_be_imported(self):
        raw = {"id": "old-dnc", "name": "Test Lead", "city": "Greensboro",
               "birthday": "1962-01-01", "phone": "3365551001", "home_phone": "3365551002", "dnc": True}
        with db.connect() as conn:
            conn.execute("INSERT INTO oscr_seen_leads(oscr_id,phone,first_seen_at) VALUES ('old-dnc','+13365551001','2026-01-01')")
            conn.execute("INSERT INTO suppressions(scope,value,reason) VALUES ('phone','+13365551001','oscr_dnc')")
        cfg = oscr.Config()
        self.assertEqual(oscr.ingest_lead(oscr.normalize_api_lead(raw, cfg), cfg)[0], "imported")
        self.assert_both()

    def test_file_import_adds_second_number_when_first_already_exists(self):
        with tempfile.TemporaryDirectory() as folder:
            contacts = str(Path(folder) / "contacts.xlsx")
            leads = str(Path(folder) / "T65 January 1962.xlsx")
            pd.DataFrame([{"Name": "Test Lead", "Phone": "+13365551001"}]).to_excel(contacts, index=False)
            pd.DataFrame([{"Name": "Test Lead", "PhoneT": "3365551001", "HomePhone": "DNC 3365551002",
                           "DNC": "YES", "HomeCity": "Greensboro"}]).to_excel(leads, index=False)
            import_lead_files.run([leads], contacts)
            import_lead_files.run([leads], contacts)
            phones = pd.read_excel(contacts, dtype=str)["Phone"].map(db.normalize_phone).tolist()
            self.assertEqual(phones, ["+13365551001", "+13365551002"])


if __name__ == "__main__":
    unittest.main()
