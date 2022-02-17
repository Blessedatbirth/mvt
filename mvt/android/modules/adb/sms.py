# Mobile Verification Toolkit (MVT)
# Copyright (c) 2021-2022 The MVT Project Authors.
# Use of this software is governed by the MVT License 1.1 that can be found at
#   https://license.mvt.re/1.1/

import logging
import os
import sqlite3
import tarfile
import io
import zlib
import base64
import json
import datetime

from mvt.common.utils import check_for_links, convert_timestamp_to_iso
from mvt.common.module import InsufficientPrivileges

from .base import AndroidExtraction

log = logging.getLogger(__name__)

SMS_BUGLE_PATH = "data/data/com.google.android.apps.messaging/databases/bugle_db"
SMS_BUGLE_QUERY = """
SELECT
    ppl.normalized_destination AS address,
    p.timestamp AS timestamp,
CASE WHEN m.sender_id IN
(SELECT _id FROM participants WHERE contact_id=-1)
THEN 2 ELSE 1 END incoming, p.text AS body
FROM messages m, conversations c, parts p,
        participants ppl, conversation_participants cp
WHERE (m.conversation_id = c._id)
    AND (m._id = p.message_id)
    AND (cp.conversation_id = c._id)
    AND (cp.participant_id = ppl._id);
"""

SMS_MMSSMS_PATH = "data/data/com.android.providers.telephony/databases/mmssms.db"
SMS_MMSMS_QUERY = """
SELECT
    address AS address,
    date_sent AS timestamp,
    type as incoming,
    body AS body
FROM sms;
"""


class SMS(AndroidExtraction):
    """This module extracts all SMS messages containing links."""

    def __init__(self, file_path=None, base_folder=None, output_folder=None,
                 serial=None, fast_mode=False, log=None, results=[]):
        super().__init__(file_path=file_path, base_folder=base_folder,
                         output_folder=output_folder, fast_mode=fast_mode,
                         log=log, results=results)

    def serialize(self, record):
        body = record["body"].replace("\n", "\\n")
        return {
            "timestamp": record["isodate"],
            "module": self.__class__.__name__,
            "event": f"sms_{record['direction']}",
            "data": f"{record['address']}: \"{body}\""
        }

    def check_indicators(self):
        if not self.indicators:
            return

        for message in self.results:
            if "body" not in message:
                continue

            message_links = check_for_links(message["body"])
            if self.indicators.check_domains(message_links):
                self.detected.append(message)

    def _parse_db(self, db_path):
        """Parse an Android bugle_db SMS database file.

        :param db_path: Path to the Android SMS database file to process

        """
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()

        if (self.SMS_DB_TYPE == 1):
            cur.execute(SMS_BUGLE_QUERY)
        elif (self.SMS_DB_TYPE == 2):
            cur.execute(SMS_MMSMS_QUERY)

        names = [description[0] for description in cur.description]

        for item in cur:
            message = {}
            for index, value in enumerate(item):
                message[names[index]] = value

            message["direction"] = ("received" if message["incoming"] == 1 else "sent")
            message["isodate"] = convert_timestamp_to_iso(message["timestamp"])

            # If we find links in the messages or if they are empty we add
            # them to the list of results.
            if check_for_links(message["body"]) or message["body"].strip() == "":
                self.results.append(message)

        cur.close()
        conn.close()

        log.info("Extracted a total of %d SMS messages containing links", len(self.results))

    def _extract_sms_from_backup_tar(self, tar_data):
        # Extract data from generated tar file
        tar_bytes = io.BytesIO(tar_data)
        tar = tarfile.open(fileobj=tar_bytes, mode='r')
        for member in tar.getmembers():
            if not member.name.endswith("_sms_backup"):
                continue

            self.log.debug("Extracting SMS messages from backup file %s", member.name)
            sms_part_zlib = zlib.decompress(tar.extractfile(member).read())
            json_data = json.loads(sms_part_zlib)

            # TODO: Copied from SMS module. Refactor to avoid duplication
            for message in json_data:
                utc_timestamp = datetime.datetime.utcfromtimestamp(int(message["date"]) / 1000)
                message["isodate"] = convert_timestamp_to_iso(utc_timestamp)
                message["direction"] = ("sent" if int(message["date_sent"]) else "received")

                message_links = check_for_links(message["body"])
                if message_links or message["body"].strip() == "":
                    self.results.append(message)

        log.info("Extracted a total of %d SMS messages containing links", len(self.results))

    def _extract_sms_adb(self):
        """Use the Android backup command to extract SMS data from the native SMS app

        It is crucial to use the under-documented "-nocompress" flag to disable the non-standard Java compression
        algorithim. This module only supports an unencrypted ADB backup.
        """
        Run ADB command to create a backup of SMS app
        self.log.warning("Please check phone and accept Android backup prompt. Do not set an encryption password. \a")

        # TODO: Base64 encoding as temporary fix to avoid byte-mangling over the shell transport...
        backup_output_b64 = self._adb_command("/system/bin/bu backup -nocompress com.android.providers.telephony | base64")
        backup_output = base64.b64decode(backup_output_b64)
        if not backup_output.startswith(b"ANDROID BACKUP"):
            self.log.error("Extracting SMS via Android backup failed. No valid backup data found.")
            return

        [magic_header, version, is_compressed, encryption, tar_data] = backup_output.split(b"\n", 4)
        if encryption != b"none" or int(is_compressed):
            self.log.error("The backup is encrypted or compressed and cannot be parsed. "
                           "[version: %s, encryption: %s, compression: %s]", version, encryption, is_compressed)
            return

        self._extract_sms_from_backup_tar(tar_data)

    def run(self):
        try:
            if (self._adb_check_file_exists(os.path.join("/", SMS_BUGLE_PATH))):
                self.SMS_DB_TYPE = 1
                self._adb_process_file(os.path.join("/", SMS_BUGLE_PATH), self._parse_db)
            elif (self._adb_check_file_exists(os.path.join("/", SMS_MMSSMS_PATH))):
                self.SMS_DB_TYPE = 2
                self._adb_process_file(os.path.join("/", SMS_MMSSMS_PATH), self._parse_db)
            return
        except InsufficientPrivileges:
            pass

        self.log.warn("No SMS database found. Trying extraction of SMS data using Android backup feature.")
        self._extract_sms_adb()
