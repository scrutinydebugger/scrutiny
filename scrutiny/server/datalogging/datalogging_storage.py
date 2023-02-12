#    datalogging_storage.py
#        A storage interface to save and fetch datalogging acquisition from the disk to keep
#        an history of them
#
#   - License : MIT - See LICENSE file.
#   - Project :  Scrutiny Debugger (github.com/scrutinydebugger/scrutiny-python)
#
#   Copyright (c) 2021-2023 Scrutiny Debugger

import os
import appdirs  # type: ignore
import tempfile
import logging
import traceback
from scrutiny.server.datalogging.definitions.api import DataloggingAcquisition, DataSeries, AxisDefinition
from pathlib import Path

import sqlite3
from datetime import datetime
from typing import Optional, Dict, List


class BadVersionError(Exception):
    version: int

    def __init__(self, version, *args, **kwargs):
        self.version = version
        super().__init__(*args, **kwargs)


class TempStorageWithAutoRestore:
    storage: "DataloggingStorageManager"

    def __init__(self, storage: "DataloggingStorageManager"):
        self.storage = storage

    def __enter__(self) -> "TempStorageWithAutoRestore":
        return self

    def __exit__(self, type, value, traceback):
        self.restore()

    def restore(self):
        self.storage.restore_storage()


class SQLiteSession:
    storage: "DataloggingStorageManager"
    conn: Optional[sqlite3.Connection]

    def __init__(self, storage: "DataloggingStorageManager"):
        self.storage = storage
        self.conn = None

    def __enter__(self) -> sqlite3.Connection:
        self.conn = sqlite3.connect(self.storage.get_db_filename())
        return self.conn

    def __exit__(self, type, value, traceback):
        if self.conn is not None:
            self.conn.close()


class DataloggingStorageManager:
    FILENAME = "scrutiny_datalog.sqlite"
    STORAGE_VERSION = 1  # Update this if the structure of the DB changes. Keep an integer

    folder: str
    temporary_dir: Optional[tempfile.TemporaryDirectory]
    logger: logging.Logger
    unavailable: bool

    def __init__(self, folder):
        self.folder = folder
        self.temporary_dir = None
        self.logger = logging.getLogger(self.__class__.__name__)
        self.unavailable = True
        os.makedirs(self.folder, exist_ok=True)

    def use_temp_storage(self) -> TempStorageWithAutoRestore:
        """Require the storage manager to switch to a temporary directory. Used for unit testing"""
        self.temporary_dir = tempfile.TemporaryDirectory()  # Directory is deleted when this object is destroyed. Need to keep a reference.
        self.initialize()
        return TempStorageWithAutoRestore(self)

    def restore_storage(self) -> None:
        """Require the storage manager to work on the real directory and not a temporary directory"""
        self.temporary_dir = None

    def get_storage_dir(self) -> str:
        """Ge the actual storage directory"""
        if self.temporary_dir is not None:
            return self.temporary_dir.name
        else:
            return self.folder

    def get_db_filename(self) -> str:
        return os.path.join(self.get_storage_dir(), self.FILENAME)

    def clear_all(self) -> None:
        filename = self.get_db_filename()
        if os.path.isfile(filename):
            os.remove(filename)

    def initialize(self) -> None:
        self.logger.debug('Initializing datalogging storage. DB file at %s' % self.get_db_filename())
        self.unavailable = True
        err: Optional[Exception] = None

        try:
            try:
                with SQLiteSession(self) as conn:
                    self.check_version(conn)
            except BadVersionError as e:
                self.backup_db(e.version)

            with SQLiteSession(self) as conn:
                self.create_db_if_not_exists(conn)
            self.unavailable = False
            self.logger.debug('Datalogging storage ready')
        except Exception as e:
            self.logger.error('Failed to initialize datalogging storage. Resetting storage at %s. %s' % (self.get_db_filename(), str(e)))
            self.logger.debug(traceback.format_exc())
            err = e

        if err:
            try:
                self.clear_all()
                self.logger.debug('Datalogging storage cleared')
            except Exception as e:
                self.logger.error("Failed to reset storage. Datalogging storage will not be accessible. %s" % str(e))
                self.logger.debug(traceback.format_exc())
                return

            try:
                with SQLiteSession(self) as conn:
                    self.create_db_if_not_exists(conn)
                self.unavailable = False
                self.logger.debug('Datalogging storage ready')
            except Exception as e:
                self.logger.error('Failed to initialize datalogging storage a 2nd time. Datalogging storage will not be accessible. %s' % str(e))
                self.logger.debug(traceback.format_exc())

    def make_meta_table_if_not_exists(self, conn: sqlite3.Connection) -> None:
        cursor = conn.cursor()
        cursor.execute(
            """ 
            CREATE TABLE IF NOT EXISTS `meta` (
                `field` VARCHAR(255) PRIMARY KEY,
                `value` TEXT NOT NULL
            )
            """
        )

    def read_version(self, conn: sqlite3.Connection) -> Optional[int]:
        self.make_meta_table_if_not_exists(conn)

        cursor = conn.cursor()
        cursor.execute("SELECT `value` FROM `meta` WHERE `field`='storage_version'")
        rows = cursor.fetchall()
        if len(rows) > 1:
            raise ValueError('More than 1 version was returned by the database.')

        if len(rows) == 0:
            return None

        return int(rows[0][0])

    def check_version(self, conn: sqlite3.Connection):
        read_version = self.read_version(conn)
        if read_version is None:
            self.write_version(conn)
        else:
            if read_version != self.STORAGE_VERSION:
                self.logger.warning('Storage version mismatch.')
                raise BadVersionError(read_version, "Read version ws %d. Expected %d" % (read_version, self.STORAGE_VERSION))

    def write_version(self, conn: sqlite3.Connection):
        conn.cursor().execute("INSERT INTO meta (field, value) VALUES ('storage_version', ?)", (self.STORAGE_VERSION,))
        conn.commit()

    def backup_db(self, previous_version: int) -> None:
        storage_file_path = Path(self.get_db_filename())
        backup_file = os.path.join(storage_file_path.parent, 'datalogging_storage_v%d_backup%s' % (previous_version, storage_file_path.suffix))
        if os.path.isfile(str(storage_file_path)):
            try:
                os.rename(str(storage_file_path), backup_file)
                self.logger.info("Datalogging storage version will upgrade to V%d. Old file backed up here: %s" %
                                 (self.STORAGE_VERSION, backup_file))
            except Exception as e:
                self.logger.error("Failed to backup old storage. %s" % str(e))

    def create_db_if_not_exists(self, conn: sqlite3.Connection) -> None:
        cursor = conn.cursor()
        self.make_meta_table_if_not_exists(conn)
        read_version = self.read_version(conn)
        if read_version is None:
            self.write_version(conn)

        cursor.execute(""" 
            CREATE TABLE IF NOT EXISTS `acquisitions` (
            `id` INTEGER PRIMARY KEY AUTOINCREMENT,
            `reference_id` VARCHAR(32) UNIQUE NOT NULL,
            `name` VARCHAR(255) NULL DEFAULT NULL,
            `firmware_id` VARCHAR(32)  NOT NULL,
            `timestamp` TIMESTAMP NOT NULL DEFAULT 'NOW()'
        ) 
        """)

        cursor.execute(""" 
            CREATE TABLE IF NOT EXISTS `axis` (
            `id` INTEGER PRIMARY KEY AUTOINCREMENT,
            `acquisition_id` INTEGER NOT NULL,
            `external_id` INTEGER NOT NULL,
            `is_xaxis` INTEGER NOT NULL,
            `name` VARCHAR(255)
        ) 
        """)

        cursor.execute(""" 
            CREATE TABLE IF NOT EXISTS `dataseries` (
            `id` INTEGER PRIMARY KEY AUTOINCREMENT,
            `name` VARCHAR(255),
            `logged_element` TEXT,
            `axis_id` INTEGER NULL,
            `data` BLOB  NOT NULL
        ) 
        """)

        cursor.execute(""" 
            CREATE INDEX IF NOT EXISTS `idx_axis_acquisition_id` 
            ON `axis` (`acquisition_id`)
        """)

        cursor.execute(""" 
            CREATE INDEX IF NOT EXISTS `idx_axis_ref_external_id` 
            ON `axis` (`acquisition_id`, `external_id`)
        """)

        cursor.execute(""" 
            CREATE INDEX IF NOT EXISTS `idx_axis_acquisition_id` 
            ON `axis` (`acquisition_id`)
        """)

        cursor.execute(""" 
            CREATE INDEX IF NOT EXISTS `idx_dataseries_axis_id` 
            ON `dataseries` (`axis_id`)
        """)

        conn.commit()

    def get_session(self) -> SQLiteSession:
        if self.unavailable:
            raise RuntimeError('Datalogging Storage is not accessible.')
        return SQLiteSession(self)

    def save(self, acquisition: DataloggingAcquisition) -> None:
        self.logger.debug("Saving acquisition with reference_id=%s" % (str(acquisition.reference_id)))
        if acquisition.xdata is None:
            raise ValueError("Missing X-Axis data")

        with self.get_session() as conn:
            cursor = conn.cursor()
            ts: Optional[int] = None
            if acquisition.acq_time is not None:
                ts = int(acquisition.acq_time.timestamp())

            cursor.execute(
                """
                INSERT INTO `acquisitions` 
                    (`reference_id`, `name`, `firmware_id`, `timestamp`)
                VALUES (?, ?, ?, ?)
                """,
                (
                    acquisition.reference_id,
                    acquisition.name,
                    acquisition.firmware_id,
                    ts
                )
            )

            if cursor.lastrowid is None:
                raise RuntimeError('Failed to insert Acquisition in DB')
            acquisition_db_id = cursor.lastrowid

            axis_sql = """
                INSERT INTO `axis`
                    (`acquisition_id`, `external_id`, `name`, 'is_xaxis' )
                VALUES (?,?,?,?)
                """
            axis_to_id_map: Dict[AxisDefinition, int] = {}
            all_axis = acquisition.get_unique_yaxis_list()
            for axis in all_axis:
                if axis.external_id == -1:
                    raise ValueError("Axis External ID cannot be -1, reserved value.")
                cursor.execute(axis_sql, (acquisition_db_id, axis.external_id, axis.name, 0))
                if cursor.lastrowid is None:
                    raise RuntimeError('Failed to insert axis %s in DB', str(axis.name))
                axis_to_id_map[axis] = cursor.lastrowid

            cursor.execute(axis_sql, (acquisition_db_id, -1, 'X-Axis', 1))
            x_axis_db_id = cursor.lastrowid
            if x_axis_db_id is None:
                raise RuntimeError('Failed to insert X-Axis in DB')

            data_series_sql = """
                INSERT INTO `dataseries`
                    (`name`, `logged_element`, `axis_id`, `data`)
                VALUES (?,?,?,?)
            """

            for data in acquisition.get_data():
                cursor.execute(data_series_sql, (
                    data.series.name,
                    data.series.logged_element,
                    axis_to_id_map[data.axis],
                    data.series.get_data_binary())
                )

            cursor.execute(data_series_sql, (
                acquisition.xdata.name,
                acquisition.xdata.logged_element,
                x_axis_db_id,
                acquisition.xdata.get_data_binary())
            )

            conn.commit()

    def count(self, firmware_id: Optional[str] = None) -> int:
        with self.get_session() as conn:
            cursor = conn.cursor()
            nout = 0
            if firmware_id is None:
                sql = "SELECT COUNT(1) AS n FROM `acquisitions`"
                cursor.execute(sql)
                nout = cursor.fetchone()[0]
            else:
                sql = "SELECT COUNT(1) AS n FROM `acquisitions` WHERE `firmware_id`=?"
                cursor.execute(sql, (firmware_id,))
                nout = cursor.fetchone()[0]

        return nout

    def list(self, firmware_id: Optional[str] = None) -> List[str]:
        with self.get_session() as conn:
            cursor = conn.cursor()
            listout: List[str]
            if firmware_id is None:
                sql = "SELECT `reference_id` FROM `acquisitions`"
                cursor.execute(sql)
                listout = [row[0] for row in cursor.fetchall()]
            else:
                sql = "SELECT `reference_id` FROM `acquisitions` WHERE `firmware_id`=?"
                cursor.execute(sql, (firmware_id,))
                listout = [row[0] for row in cursor.fetchall()]

        return listout

    def read(self, reference_id: str) -> DataloggingAcquisition:
        with self.get_session() as conn:
            sql = """
                SELECT 
                    `acq`.`reference_id` AS `reference_id`,
                    `acq`.`firmware_id` AS `firmware_id`,
                    `acq`.`timestamp` AS `timestamp`,
                    `acq`.`name` AS `name`,
                    `axis`.`name` AS `axis_name`,
                    `axis`.`external_id` AS `axis_external_id`,
                    `axis`.`is_xaxis` AS `is_xaxis`,
                    `ds`.`axis_id` AS `axis_id`,
                    `ds`.`name` AS `dataseries_name`,
                    `ds`.`logged_element` AS `logged_element`,
                    `ds`.`data` AS `data`
                FROM `acquisitions` AS `acq`
                LEFT JOIN `axis` AS `axis` ON `axis`.`acquisition_id`=`acq`.`id`
                INNER JOIN `dataseries` AS `ds` ON `ds`.`axis_id`=`axis`.`id`
                where `acq`.`reference_id`=?
            """
            # SQLite doesn't let us index by name
            cols = [
                'reference_id',
                'firmware_id',
                'timestamp',
                'acquisition_name',
                'axis_name',
                'axis_external_id',
                'is_xaxis',
                'axis_id',
                'dataseries_name',
                'logged_element',
                'data'
            ]
            colmap: Dict[str, int] = {}
            for i in range(len(cols)):
                colmap[cols[i]] = i

            cursor = conn.cursor()
            cursor.execute(sql, (reference_id,))

            rows = cursor.fetchall()
        if len(rows) == 0:
            raise LookupError('No acquisition identified by ID %s' % str(reference_id))

        acq = DataloggingAcquisition(
            reference_id=rows[0][colmap['reference_id']],
            firmware_id=rows[0][colmap['firmware_id']],
            acq_time=datetime.fromtimestamp(rows[0][colmap['timestamp']]),
            name=rows[0][colmap['acquisition_name']]
        )

        yaxis_id_to_def_map: Dict[int, AxisDefinition] = {}

        for row in rows:
            name = row[colmap['dataseries_name']]
            logged_element = row[colmap['logged_element']]
            data = row[colmap['data']]

            if name is None or logged_element is None or data is None:
                raise LookupError('Incomplete data in database')

            dataseries = DataSeries(name=name, logged_element=logged_element)
            dataseries.set_data_binary(data)

            if row[colmap['axis_id']] is not None:
                if not row[colmap['is_xaxis']]:  # Y-Axis
                    axis: AxisDefinition
                    if row[colmap['axis_id']] in yaxis_id_to_def_map:
                        axis = yaxis_id_to_def_map[row[colmap['axis_id']]]
                    else:
                        axis = AxisDefinition(name=row[colmap['axis_name']], external_id=row[colmap['axis_external_id']])
                        yaxis_id_to_def_map[row[colmap['axis_id']]] = axis
                    acq.add_data(dataseries, axis)
                else:
                    acq.set_xdata(dataseries)

        if acq.xdata is None:
            raise LookupError("No X-Axis in acquisition")

        return acq

    def delete(self, reference_id: str) -> None:
        with self.get_session() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                DELETE FROM `dataseries` WHERE axis_id IN (
                    SELECT `axis`.`id` FROM `axis`
                    INNER JOIN `acquisitions` as `acq` on `acq`.`id`=`axis`.`acquisition_id`
                    WHERE `acq`.`reference_id`=?
                )
                """, (reference_id,))

            cursor.execute("""
                DELETE FROM `axis` WHERE `acquisition_id` IN (
                    SELECT `id` FROM `acquisitions` WHERE `reference_id`=?
                )
                """, (reference_id,))

            cursor.execute("DELETE FROM `acquisitions` WHERE reference_id=?", (reference_id,))
            if cursor.rowcount == 0:
                raise LookupError('No acquisition identified by ID %s' % str(reference_id))

            conn.commit()

    def update_acquisition_name(self, reference_id: str, name: str) -> None:
        with self.get_session() as conn:
            cursor = conn.cursor()

            cursor.execute("""
            UPDATE `acquisitions` set `name`=? where `reference_id`=?
            """, (name, reference_id))

            if cursor.rowcount == 0:
                raise LookupError('No acquisition identified by ID %s' % str(reference_id))

            conn.commit()

    def update_axis_name(self, reference_id: str, axis_id: int, new_name: str) -> None:
        with self.get_session() as conn:
            cursor = conn.cursor()

            cursor.execute("""
            UPDATE `axis` SET `name`=? WHERE `id` IN (
                SELECT `axis`.`id` FROM `axis` 
                INNER JOIN `acquisitions` AS `acq` ON `acq`.`id`=`axis`.`acquisition_id`
                WHERE `acq`.`reference_id`=? AND `axis`.`external_id`=?
            )
            """, (new_name, reference_id, axis_id))

            if cursor.rowcount == 0:
                raise LookupError('No acquisition identified by ID %s' % str(reference_id))

            conn.commit()


GLOBAL_STORAGE = appdirs.user_data_dir('scrutiny')
DataloggingStorage = DataloggingStorageManager(GLOBAL_STORAGE)
