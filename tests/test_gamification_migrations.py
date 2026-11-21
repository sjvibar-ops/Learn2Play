"""Inspect Alembic history without connecting to a database or running upgrades."""
import ast
from pathlib import Path
import unittest

from alembic.script import ScriptDirectory
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable
from models import AttendanceEvent, XpEvent

ROOT=Path(__file__).resolve().parents[1]


class GamificationMigrationGraphTests(unittest.TestCase):
    def test_single_head_preserves_both_chains(self):
        script=ScriptDirectory(str(ROOT/'migrations'))
        self.assertEqual(script.get_heads(),['db71a6e5c902'])
        self.assertEqual(script.get_revision('c7e4a1b92d60').down_revision,'b9d3e7f02c84')
        self.assertEqual(set(script.get_revision('e6a1c9d84b20').down_revision),{'c1a82b7d43ef','d2b7a4e9310c'})
        self.assertEqual(script.get_revision('c1a82b7d43ef').down_revision,'9e45f7e7aa32')
        self.assertEqual(script.get_revision('c4b8a29f71d2').down_revision,'9e45f7e7aa32')
        self.assertEqual(script.get_revision('f31a6e8c9b24').down_revision,'c4b8a29f71d2')
        self.assertEqual(script.get_revision('d2b7a4e9310c').down_revision,'f31a6e8c9b24')

    def test_postgres_models_compile_and_xp_uniqueness_is_present(self):
        xp=str(CreateTable(XpEvent.__table__).compile(dialect=postgresql.dialect()))
        audit=str(CreateTable(AttendanceEvent.__table__).compile(dialect=postgresql.dialect()))
        self.assertIn('UNIQUE (appointment_id)',xp)
        self.assertIn('REFERENCES appointments (id)',xp)
        self.assertIn('REFERENCES users (id)',audit)
        self.assertIn('points > 0',xp)

    def test_upgrades_are_additive_and_security_is_fail_closed(self):
        for path in (ROOT/'migrations/versions').glob('*.py'):
            if path.name.startswith(('0720','9e45')): continue
            tree=ast.parse(path.read_text(encoding='utf-8'))
            upgrade=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='upgrade')
            text=ast.unparse(upgrade)
            self.assertNotIn('drop_table',text,path.name)
            self.assertNotIn('drop_column',text,path.name)
        security=(ROOT/'migrations/versions/b9d3e7f02c84_secure_backend_progress_tables.py').read_text()
        self.assertIn('ENABLE ROW LEVEL SECURITY',security)
        self.assertIn("('anon', 'authenticated')",security)
        self.assertIn('REVOKE ALL',security)
