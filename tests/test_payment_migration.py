"""Migration review checks without executing any migration function."""
import ast
import unittest
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from models import Payment
from extensions import db


class PaymentMigrationTests(unittest.TestCase):
    def test_upgrade_preserves_legacy_rows_and_enforces_constraints(self):
        path = Path(__file__).resolve().parents[1] / 'migrations/versions/c1a82b7d43ef_manual_payments.py'
        tree = ast.parse(path.read_text(encoding='utf-8'))
        upgrade = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'upgrade')
        code = ast.unparse(upgrade)
        self.assertIn("op.add_column('appointments'", code)
        self.assertIn('server_default=sa.false()', code)
        self.assertNotIn('drop_', code)
        self.assertNotIn('DELETE', code)
        # Exercise model constraints in a new in-memory database, not a migration.
        engine = sa.create_engine('sqlite://')
        with engine.begin() as connection:
            connection.exec_driver_sql('PRAGMA foreign_keys=ON')
            db.metadata.create_all(connection)
            connection.execute(db.metadata.tables['users'].insert(), [dict(id=1,display_name='Test',email='test@example.test',password_hash='test')])
            from datetime import datetime
            connection.execute(db.metadata.tables['appointments'].insert(), [dict(id=1,provider_id=1,student_id=1,topic='Legacy',start_time=datetime.now())])
            self.assertEqual(connection.exec_driver_sql('SELECT payment_required FROM appointments WHERE id=1').scalar(), 0)
            record=dict(appointment_id=1,student_id=1,amount=123.45,reference='REF')
            connection.execute(Payment.__table__.insert(),record)
            with self.assertRaises(IntegrityError):
                connection.execute(Payment.__table__.insert(),dict(record,reference='DIFFERENT'))
            connection.execute(db.metadata.tables['appointments'].insert(), [dict(id=2,provider_id=1,student_id=1,topic='Other',start_time=datetime.now())])
            with self.assertRaises(IntegrityError):
                connection.execute(Payment.__table__.insert(),dict(record,appointment_id=2))
            self.assertEqual(connection.exec_driver_sql('SELECT count(*) FROM payments').scalar(), 1)
        engine.dispose()
