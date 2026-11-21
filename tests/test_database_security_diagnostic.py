"""Offline diagnostic tests: fake connections only; no CLI launch or real SQL.

Load reviewed definitions from AST with the __main__ block excluded. The only
real driver utility used is its local conninfo parser, never psycopg.connect.
"""
import ast
import contextlib
import io
import json
import re
import sys
import types
import unittest
from pathlib import Path
from unittest import mock
from psycopg.conninfo import conninfo_to_dict

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts/audit_database_security.py"


def definitions():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    allowed = {"argparse", "getpass", "json", "os", "re", "sys", "warnings"}
    for node in tree.body:
        if isinstance(node, ast.Import):
            assert all(item.name in allowed for item in node.names)
        else:
            assert isinstance(node, (ast.Assign, ast.ClassDef, ast.FunctionDef, ast.Expr, ast.If))
    module = types.ModuleType("offline_database_diagnostic")
    tree.body = [n for n in tree.body if not isinstance(n, ast.If)]
    exec(compile(tree, str(SOURCE), "exec"), module.__dict__)
    return module


class OfflineError(Exception):
    sqlstate = "42501"


class Connection:
    def __init__(self):
        self.read_only = False
        self.isolation_level = None
        self.rollbacks = 0
        self.closed = False
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.rollback()
        self.closed = True
    def rollback(self):
        self.rollbacks += 1
    def close(self):
        self.closed = True


class DiagnosticOfflineTests(unittest.TestCase):
    def setUp(self):
        self.tool = definitions()
        self.identity = dict(database_name="appdb", current_role="flask_sql_login",
            session_role="flask_sql_login", session_authorization="flask_sql_login",
            current_schema="pg_catalog", effective_schemas=["pg_catalog"], server_version=160000,
            transaction_read_only="on", default_read_only="on", diagnostic_isolation="repeatable read",
            configured_default_isolation="read committed", row_security_setting="on", is_replica=False,
            catalog_lookup_path_verified=True, replication_role="origin", can_inspect_ssl_transport=True, can_inspect_gss_transport=True)
        self.connection = Connection()
        self.connect = mock.Mock(return_value=self.connection)
        fake = types.ModuleType("psycopg")
        fake.connect = self.connect
        fake.Error = OfflineError
        fake.IsolationLevel = types.SimpleNamespace(REPEATABLE_READ="repeatable read")
        parser = types.ModuleType("psycopg.conninfo")
        parser.conninfo_to_dict = conninfo_to_dict
        rows = types.ModuleType("psycopg.rows")
        rows.dict_row = object()
        self.imports = {"psycopg":fake,"psycopg.conninfo":parser,"psycopg.rows":rows}
        self.tool.os = types.SimpleNamespace(environ={})
        self.tool.sys = types.SimpleNamespace(stdin=types.SimpleNamespace(isatty=lambda:True),stderr=io.StringIO())
        self.tool.input = lambda prompt: "APPROVE READ ONLY"
        self.tool.getpass = types.SimpleNamespace(getpass=lambda prompt:"host=db.example.test dbname=appdb user=flask_sql_login password=OFFLINE_FAKE_PASSWORD",
                                               GetPassWarning=UserWarning)
        self.argv = ["--allow-connect","--expected-host","db.example.test","--expected-database","appdb"]
        self.called = []
        self.error_section = None
        self.truncated = None
        def inventory(connection, sql, parameters, limit):
            self.assertTrue(connection.read_only)
            self.assertEqual(connection.isolation_level,"repeatable read")
            if sql == self.tool.IDENTITY:
                self.called.append("identity")
                return dict(status="ok",rows=[dict(self.identity)],truncated=False)
            name = next(n for n,q in self.tool.QUERIES.items() if q == sql)
            self.called.append(name)
            if name == self.error_section:
                raise OfflineError("OFFLINE_ERROR_MUST_NOT_BE_PRINTED")
            result = []
            if name == "ssl_transport": result = [dict(server_leg_ssl=True,tls_version="TLSv1.3",cipher="offline-cipher",cipher_bits=256)]
            if name == "gss_transport": result = [dict(gss_authenticated=False,server_leg_gss_encrypted=False)]
            return dict(status="ok",rows=result,truncated=name==self.truncated)
        self.tool.inspect_query = inventory

    def run_fake(self, extra=None):
        output = io.StringIO()
        with mock.patch.dict(sys.modules,self.imports), contextlib.redirect_stdout(output):
            code = self.tool.main(self.argv if extra is None else extra)
        self.output = output.getvalue()
        return code,json.loads(self.output.splitlines()[-1])

    def expect_refused(self):
        with mock.patch.dict(sys.modules,self.imports), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(self.tool.DiagnosticError): self.tool.main(self.argv)
        self.connect.assert_not_called()

    def run_cli_fake(self):
        output,error=io.StringIO(),io.StringIO()
        self.tool.sys.stderr=error
        with mock.patch.dict(sys.modules,self.imports), contextlib.redirect_stdout(output):
            code=self.tool.run_cli(self.argv)
        self.assertNotIn("OFFLINE_FAKE_PASSWORD",output.getvalue()+error.getvalue())
        self.assertNotIn("DO_NOT_RENDER_EXCEPTION",output.getvalue()+error.getvalue())
        return code,error.getvalue()

    def test_safe_cli_stages_for_prompt_parsing_configuration_and_connection_errors(self):
        for stage in ("credential_input","url_parsing","connection_configuration","connection_open"):
            with self.subTest(stage=stage):
                self.setUp()
                if stage=="credential_input":
                    self.tool.getpass.getpass=mock.Mock(side_effect=EOFError("DO_NOT_RENDER_EXCEPTION"))
                elif stage=="url_parsing":
                    parser=self.imports["psycopg.conninfo"]
                    parser.conninfo_to_dict=mock.Mock(side_effect=ValueError("DO_NOT_RENDER_EXCEPTION"))
                elif stage=="connection_configuration":
                    self.tool.getpass.getpass=lambda prompt:"host=db.example.test dbname=appdb user=u port=invalid"
                else:
                    self.connect.side_effect=OfflineError("DO_NOT_RENDER_EXCEPTION")
                code,error=self.run_cli_fake()
                self.assertEqual(code,2)
                self.assertIn("stage="+stage+";",error)
                self.assertIn("category="+self.tool.FAILURE_CATEGORIES[stage],error)
                self.assertEqual(self.called,[])
                if stage!="connection_open":self.connect.assert_not_called()

    def test_safe_cli_reports_failed_verifications_without_inventory(self):
        for stage,key,value in (("read_only_verification","transaction_read_only","off"),
                                ("startup_verification","catalog_lookup_path_verified",False),
                                ("database_verification","database_name","wrong"),
                                ("runtime_role_verification","current_role","wrong")):
            with self.subTest(stage=stage):
                self.setUp()
                self.identity[key]=value
                self.argv += ["--expected-runtime-role","flask_sql_login"]
                code,error=self.run_cli_fake()
                self.assertEqual(code,2)
                self.assertIn("stage="+stage+"; category=safety_check_failed",error)
                self.assertEqual(self.called,["identity"])
                self.assertTrue(self.connection.closed)

    def test_safe_cli_identity_assessment_rollback_and_cleanup_failures(self):
        for stage in ("identity_query","assessment","rollback","connection_cleanup"):
            with self.subTest(stage=stage):
                self.setUp()
                failure=RuntimeError("DO_NOT_RENDER_EXCEPTION")
                if stage=="identity_query":self.tool.inspect_query=mock.Mock(side_effect=failure)
                elif stage=="assessment":self.tool.assess=mock.Mock(side_effect=failure)
                elif stage=="rollback":
                    # Explicit rollback fails; context cleanup then succeeds.
                    self.connection.rollback=mock.Mock(side_effect=[failure,None])
                else:
                    self.connection.rollback=mock.Mock(side_effect=[None,failure])
                code,error=self.run_cli_fake()
                self.assertEqual(code,2)
                self.assertIn("stage="+stage+";",error)
                self.assertIn("category="+self.tool.FAILURE_CATEGORIES[stage],error)

    def test_safe_cli_preserves_consent_tls_and_successful_cleanup(self):
        self.argv.remove("--allow-connect")
        code,error=self.run_cli_fake()
        self.assertEqual(code,2)
        self.connect.assert_not_called()
        self.setUp()
        self.tool.input=lambda prompt:"APPROVE READ ONLY "
        code,error=self.run_cli_fake()
        self.assertEqual(code,2)
        self.connect.assert_not_called()
        self.setUp()
        self.tool.getpass.getpass=lambda prompt:"host=db.example.test dbname=appdb user=u sslmode=require"
        code,error=self.run_cli_fake()
        self.assertEqual(code,2)
        self.assertIn("stage=tls_configuration",error)
        self.connect.assert_not_called()
        self.setUp()
        code,error=self.run_cli_fake()
        self.assertEqual(code,0)
        self.assertEqual(error,"")
        self.assertTrue(self.connection.closed)
        self.assertGreaterEqual(self.connection.rollbacks,1)

    def test_safe_cli_never_formats_connection_exception_and_handles_interrupt(self):
        class UnprintableError(Exception):
            def __str__(self):
                raise AssertionError("Exception text must never be inspected")
        self.connect.side_effect=UnprintableError()
        code,error=self.run_cli_fake()
        self.assertEqual(code,2)
        self.assertIn("category=connection_tls_auth_or_startup_failed",error)
        self.setUp()
        self.tool.getpass.getpass=mock.Mock(side_effect=KeyboardInterrupt("DO_NOT_RENDER_EXCEPTION"))
        code,error=self.run_cli_fake()
        self.assertEqual(code,2)
        self.assertIn("stage=credential_input; category=interrupted",error)
        self.connect.assert_not_called()

    def test_safe_cli_preserves_catalog_error_partial_report(self):
        self.error_section="roles"
        code,error=self.run_cli_fake()
        self.assertEqual(code,2)
        self.assertIn("stage=catalog_inventory; category=safety_check_failed",error)
        self.assertNotIn("tables",self.called)
        self.assertTrue(self.connection.closed)

    def test_read_only_failure_reports_only_allowlisted_states_and_stops(self):
        for transaction,default in (("on","off"),("off","on"),("off","off"),
                                    (None,"on"),(True,"on"),(b"on","on"),
                                    ("DO_NOT_RENDER_EXCEPTION","on"),("on",None)):
            with self.subTest(transaction_type=type(transaction).__name__,default_type=type(default).__name__):
                self.setUp()
                self.identity.update(transaction_read_only=transaction,default_read_only=default)
                code,error=self.run_cli_fake()
                self.assertEqual(code,2)
                states=json.loads(error.splitlines()[0])["read_only_verification"]
                for key,value in (("transaction_read_only",transaction),("default_read_only",default)):
                    expected=value if isinstance(value,str) and value in ("on","off") else "unexpected"
                    self.assertEqual(states[key],expected)
                self.assertIn("stage=read_only_verification; category=safety_check_failed",error)
                self.assertEqual(self.called,["identity"])
                self.assertTrue(self.connection.closed)
                self.assertGreaterEqual(self.connection.rollbacks,1)

    def test_read_only_success_is_silent_and_identity_query_error_is_distinct(self):
        code,error=self.run_cli_fake()
        self.assertEqual(code,0)
        self.assertEqual(error,"")
        self.setUp()
        self.tool.inspect_query=mock.Mock(side_effect=OfflineError("DO_NOT_RENDER_EXCEPTION"))
        code,error=self.run_cli_fake()
        self.assertEqual(code,2)
        self.assertIn("stage=identity_query; category=identity_query_or_result_failed",error)
        self.assertNotIn('"read_only_verification"',error)
        self.assertTrue(self.connection.closed)

    def test_installed_psycopg_constructs_explicit_read_only_transaction_offline(self):
        # Exercise only the installed driver's pure command builder. No real
        # Connection is instantiated, no credential read, no SQL executed.
        from psycopg import IsolationLevel
        from psycopg._connection_base import BaseConnection
        state=types.SimpleNamespace(_begin_statement=b"",isolation_level=IsolationLevel.REPEATABLE_READ,
                                    read_only=True,deferrable=None)
        self.assertEqual(BaseConnection._get_tx_start_command(state),
                         b"BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")

    def test_cleanup_attempts_rollback_then_close_and_preserves_original_failure(self):
        class UnprintableError(Exception):
            def __str__(self):raise AssertionError("Cleanup exception must not be formatted")
        for original_kind in ("none","diagnostic","unexpected","interrupt"):
            for rollback_fails,close_fails in ((False,False),(True,False),(False,True),(True,True)):
                with self.subTest(original=original_kind,rollback=rollback_fails,close=close_fails):
                    progress={"stage":"read_only_verification"}
                    calls=[]
                    connection=mock.MagicMock()
                    def rollback():
                        calls.append("rollback")
                        if rollback_fails:raise UnprintableError()
                    def close():
                        calls.append("close")
                        if close_fails:raise UnprintableError()
                    connection.rollback.side_effect=rollback
                    connection.close.side_effect=close
                    original={"none":None,"diagnostic":self.tool.DiagnosticError("Original fixed failure."),
                              "unexpected":UnprintableError(),"interrupt":KeyboardInterrupt()}[original_kind]
                    caught=None
                    with mock.patch("logging.Logger._log") as log:
                        try:
                            with self.tool.ConnectionScope(connection,progress) as active:
                                self.assertIs(active,connection)
                                if original is not None:raise original
                        except BaseException as exc:
                            caught=exc
                        log.assert_not_called()
                    connection.__enter__.assert_not_called()
                    connection.__exit__.assert_not_called()
                    self.assertEqual(calls,["rollback","close"])
                    self.assertEqual(progress.get("cleanup_failed",False),rollback_fails or close_fails)
                    if original is not None:
                        self.assertIs(caught,original)
                        self.assertEqual(progress["stage"],"read_only_verification")
                    elif rollback_fails or close_fails:
                        self.assertIsInstance(caught,RuntimeError)
                        self.assertEqual(str(caught),"Diagnostic connection cleanup failed.")
                        self.assertEqual(progress["stage"],"connection_cleanup")
                    else:
                        self.assertIsNone(caught)

    def test_read_only_failure_stays_primary_during_simultaneous_cleanup_failures(self):
        for transaction,default in (("on","off"),("off","on"),("off","off"),(None,"on")):
            with self.subTest(transaction=transaction,default=default):
                self.setUp()
                self.identity.update(transaction_read_only=transaction,default_read_only=default)
                self.connection.rollback=mock.Mock(side_effect=RuntimeError("DO_NOT_RENDER_EXCEPTION"))
                self.connection.close=mock.Mock(side_effect=RuntimeError("DO_NOT_RENDER_EXCEPTION"))
                with mock.patch("logging.Logger._log") as log:
                    code,error=self.run_cli_fake()
                    log.assert_not_called()
                self.assertEqual(code,2)
                self.assertIn("stage=read_only_verification; category=safety_check_failed",error)
                self.assertIn("Cleanup also failed; details suppressed.",error)
                states=json.loads(error.splitlines()[0])["read_only_verification"]
                self.assertLessEqual(set(states.values()),{"on","off","unexpected"})
                self.assertEqual(self.called,["identity"])
                self.connection.rollback.assert_called_once_with()
                self.connection.close.assert_called_once_with()

    def test_cleanup_interrupt_still_attempts_close_and_preserves_diagnostic_error(self):
        connection=mock.MagicMock()
        connection.rollback.side_effect=KeyboardInterrupt()
        progress={"stage":"assessment"}
        original=self.tool.DiagnosticError("Original fixed failure.")
        with self.assertRaises(self.tool.DiagnosticError) as caught:
            with self.tool.ConnectionScope(connection,progress):raise original
        self.assertIs(caught.exception,original)
        connection.close.assert_called_once_with()
        self.assertEqual(progress["stage"],"assessment")

    def test_requires_explicit_consent_and_interactive_terminal(self):
        for scenario in ("flag","text","tty"):
            with self.subTest(scenario=scenario):
                self.setUp()
                if scenario=="flag": self.argv.remove("--allow-connect")
                if scenario=="text": self.tool.input=lambda prompt:"no"
                if scenario=="tty": self.tool.sys.stdin.isatty=lambda:False
                self.expect_refused()

    def test_rejects_weaker_tls_modes_before_connect(self):
        for mode in ("disable","allow","prefer","require","verify-ca"):
            with self.subTest(mode=mode):
                self.tool.getpass.getpass=lambda prompt: f"host=db.example.test dbname=appdb user=flask_sql_login sslmode={mode}"
                self.expect_refused()

    def test_pins_tls_and_gss_despite_ambient_downgrade(self):
        self.tool.os.environ={"PGSSLMODE":"disable","PGGSSENCMODE":"require"}
        code,report=self.run_fake()
        self.assertEqual(code,0)
        args=self.connect.call_args.kwargs
        self.assertEqual(args["sslmode"],"verify-full")
        self.assertEqual(args["gssencmode"],"disable")
        self.assertEqual(args["host"],"db.example.test")
        self.assertEqual(args["port"],"5432")
        self.assertEqual(args["dbname"],"appdb")
        self.assertFalse(args["autocommit"])
        self.assertIn("default_transaction_read_only=on",args["options"])
        self.assertIn("search_path=pg_catalog",args["options"])
        self.assertEqual(self.called[0],"identity")
        self.assertGreaterEqual(self.connection.rollbacks,1)
        self.assertTrue(self.connection.closed)
        self.assertEqual(report["identity"]["current_role"],"flask_sql_login")
        self.assertTrue(report["sections"]["ssl_transport"]["rows"][0]["server_leg_ssl"])
        self.assertNotIn("OFFLINE_FAKE_PASSWORD",self.output)
        self.assertTrue(report["findings"])  # Exit zero is collection completeness, not security approval.

    def test_rejects_explicit_gss_and_routing_overrides(self):
        for option in ("gssencmode=prefer","gssencmode=require","service=ignored","hostaddr=127.0.0.1","options='-c default_transaction_read_only=off'","replication=true","target_session_attrs=any"):
            with self.subTest(option=option):
                self.tool.getpass.getpass=lambda prompt: "host=db.example.test dbname=appdb user=flask_sql_login "+option
                self.expect_refused()
        for key in ("PGSERVICE","PGSERVICEFILE","PGHOSTADDR","PGOPTIONS"):
            with self.subTest(key=key):
                self.tool.os.environ={key:"offline-override"}
                self.expect_refused()

    def test_rejects_target_mismatches(self):
        for dsn in ("host=other.example.test dbname=appdb user=u","host=db.example.test port=6543 dbname=appdb user=u","host=db.example.test dbname=other user=u","host=db.example.test dbname=appdb"):
            with self.subTest(dsn=dsn):
                self.tool.getpass.getpass=lambda prompt:dsn
                self.expect_refused()

    def test_checks_read_only_database_version_and_optional_role_before_inventory(self):
        for key,value in (("catalog_lookup_path_verified",False),("transaction_read_only","off"),("default_read_only","off"),("database_name","wrong"),("server_version",110000),("current_role","wrong")):
            with self.subTest(key=key):
                self.setUp()
                self.identity[key]=value
                self.argv += ["--expected-runtime-role","flask_sql_login"]
                with mock.patch.dict(sys.modules,self.imports), contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(self.tool.DiagnosticError): self.tool.main(self.argv)
                self.assertEqual(self.called,["identity"])
                self.assertTrue(self.connection.closed)
                self.assertGreaterEqual(self.connection.rollbacks,1)

    def test_unavailable_transport_is_marked_incomplete_without_querying_it(self):
        self.identity["can_inspect_ssl_transport"]=False
        code,report=self.run_fake()
        self.assertEqual(code,2)
        self.assertTrue(report["incomplete"])
        self.assertEqual(report["sections"]["ssl_transport"]["status"],"unavailable")
        self.assertNotIn("ssl_transport",self.called)
        self.assertIn("tables",self.called)

    def test_missing_transport_row_is_unavailable_not_silent_success(self):
        original=self.tool.inspect_query
        def inventory(*args):
            result=original(*args)
            if args[1]==self.tool.QUERIES["ssl_transport"]:result["rows"]=[]
            return result
        self.tool.inspect_query=inventory
        code,report=self.run_fake()
        self.assertEqual(code,2)
        self.assertEqual(report["sections"]["ssl_transport"]["status"],"unavailable")

    def test_explicit_url_mode_uses_only_the_fake_environment(self):
        self.tool.os.environ={"DATABASE_URL":"postgresql+psycopg://pooler.project:OFFLINE_FAKE_PASSWORD@db.example.test:5432/appdb?sslmode=verify-full&gssencmode=disable"}
        self.tool.getpass.getpass=mock.Mock(side_effect=AssertionError("No password prompt expected"))
        code,report=self.run_fake(self.argv+["--use-database-url","--expected-runtime-role","flask_sql_login"])
        self.assertEqual(code,0)
        self.tool.getpass.getpass.assert_not_called()
        self.assertEqual(self.connect.call_args.kwargs["user"],"pooler.project")
        self.assertEqual(report["identity"]["current_role"],"flask_sql_login")
        self.assertNotIn("OFFLINE_FAKE_PASSWORD",self.output)

    def test_truncation_is_not_reported_as_complete(self):
        self.truncated="views"
        code,report=self.run_fake()
        self.assertEqual(code,2)
        self.assertTrue(report["incomplete"])
        self.assertTrue(any(f["code"]=="INCOMPLETE_SECTION" for f in report["findings"]))

    def test_query_failure_stops_with_sanitized_partial_report(self):
        self.error_section="roles"
        output=io.StringIO()
        with mock.patch.dict(sys.modules,self.imports), contextlib.redirect_stdout(output):
            with self.assertRaises(self.tool.DiagnosticError):self.tool.main(self.argv)
        report=json.loads(output.getvalue().splitlines()[-1])
        self.assertTrue(report["incomplete"])
        self.assertEqual(report["sections"]["roles"]["sqlstate"],"42501")
        self.assertIn("tables",report["unattempted_sections"])
        self.assertNotIn("tables",self.called)
        self.assertNotIn("OFFLINE_ERROR_MUST_NOT_BE_PRINTED",output.getvalue())
        self.assertNotIn("OFFLINE_FAKE_PASSWORD",output.getvalue())
        self.assertEqual(self.connect.call_count,1)
        self.assertTrue(self.connection.closed)


class QueryAndAssessmentTests(unittest.TestCase):
    def setUp(self): self.tool=definitions()
    def empty_sections(self):return {n:dict(status="ok",truncated=False,rows=[]) for n in self.tool.QUERIES}
    def identity(self):return dict(current_role="flask_sql_login",configured_default_isolation="read committed",is_replica=False,row_security_setting="on",replication_role="origin")

    def test_every_statement_passes_guard_and_binds_limits_without_sql_execution(self):
        cursor=mock.MagicMock()
        cursor.__enter__.return_value=cursor
        cursor.fetchmany.return_value=[]
        connection=mock.Mock()
        connection.cursor.return_value=cursor
        for name,sql in [("identity",self.tool.IDENTITY),*self.tool.QUERIES.items()]:
            with self.subTest(section=name):
                self.tool.inspect_query(connection,sql,dict(tables=list(self.tool.APPLICATION_TABLES)+["alembic_version"],schema="public"),20)
                sent,params=cursor.execute.call_args.args
                self.assertTrue(sent.startswith(("SELECT","WITH","\nSELECT","\nWITH")))
                self.assertNotIn(";",sent)
                self.assertEqual(params["row_limit"],21)

    def test_bound_values_do_not_become_sql_and_truncation_is_explicit(self):
        cursor=mock.MagicMock();cursor.__enter__.return_value=cursor
        cursor.fetchmany.return_value=[{"x":1},{"x":2}]
        connection=mock.Mock();connection.cursor.return_value=cursor
        value="x'; DELETE FROM users; --"
        result=self.tool.inspect_query(connection,"SELECT %(schema)s",{"schema":value},1)
        sql,params=cursor.execute.call_args.args
        self.assertNotIn(value,sql);self.assertEqual(params["schema"],value)
        self.assertTrue(result["truncated"]);self.assertEqual(len(result["rows"]),1)

    def test_guard_rejects_common_accidental_mutations_and_non_catalog_reads(self):
        for sql in ("DELETE FROM users","SELECT 1; SELECT 2","WITH x AS (DELETE FROM users RETURNING id) SELECT * FROM x","SELECT pg_catalog.nextval('x')","SELECT pg_catalog.set_config('row_security','off',false)","SELECT app.secret()","SELECT * FROM public.users","SELECT * FROM users"):
            with self.subTest(sql=sql):
                connection=mock.Mock()
                with self.assertRaises(self.tool.DiagnosticError):self.tool.inspect_query(connection,sql,{},10)
                connection.cursor.assert_not_called()

    def test_column_only_select_and_view_writes_are_reported(self):
        sections=self.empty_sections()
        for operation in ("select","insert","update","delete"):
            for column_only in ([False,True] if operation!="delete" else [False]):
                with self.subTest(operation=operation,column_only=column_only):
                    row=dict(role_name="authenticated",schema_usage=True,schema_name="public",view_name="wrapper")
                    row.update({"can_"+op:False for op in ("select","insert","update","delete")})
                    row.update({"any_column_"+op:False for op in ("select","insert","update")})
                    row[("any_column_" if column_only else "can_")+operation]=True
                    sections["views"]["rows"]=[row]
                    findings,_=self.tool.assess(sections,self.identity(),"public")
                    result=next(f for f in findings if f["code"]=="REVIEW_CLIENT_VIEW")
                    self.assertEqual(result["operations"],[operation.upper()])

    def test_view_query_supplies_every_assessed_privilege_with_correct_catalog_check(self):
        sql=self.tool.QUERIES["views"]
        checks=re.findall(
            r"pg_catalog\.(has_table_privilege|has_any_column_privilege)"
            r"\(r\.oid,c\.oid,'(SELECT|INSERT|UPDATE|DELETE)'\) AS ([a-z_]+)",sql)
        expected={"can_"+op:("has_table_privilege",op.upper())
                  for op in ("select","insert","update","delete")}
        expected.update({"any_column_"+op:("has_any_column_privilege",op.upper())
                         for op in ("select","insert","update")})
        self.assertEqual({alias:(function,privilege) for function,privilege,alias in checks},expected)
        self.assertEqual(len(checks),len(expected))
        # Derive mock fields from the SQL so mocks cannot conceal missing aliases.
        for active in [None,*expected]:
            for role,usage in (("anon",True),("authenticated",True),
                               ("authenticated",False),("service_role",True)):
                with self.subTest(active=active,role=role,schema_usage=usage):
                    row=dict(schema_name="public",view_name="wrapper",role_name=role,schema_usage=usage)
                    row.update({alias:alias==active for _,_,alias in checks})
                    sections=self.empty_sections()
                    sections["views"]["rows"]=[row]
                    findings,_=self.tool.assess(sections,self.identity(),"public")
                    views=[f for f in findings if f["code"]=="REVIEW_CLIENT_VIEW"]
                    if active and usage and role in ("anon","authenticated"):
                        self.assertEqual(len(views),1)
                        self.assertEqual(views[0]["operations"],[expected[active][1]])
                    else:
                        self.assertEqual(views,[])

    def test_provider_history_and_all_current_models_are_covered(self):
        tree=ast.parse((ROOT/"models.py").read_text(encoding="utf-8"))
        tables=set()
        for cls in tree.body:
            if isinstance(cls,ast.ClassDef):
                for n in cls.body:
                    if isinstance(n,ast.Assign) and any(isinstance(k,ast.Name) and k.id=="__tablename__" for k in n.targets):tables.add(ast.literal_eval(n.value))
        self.assertEqual(set(self.tool.APPLICATION_TABLES),tables)
        self.assertEqual(len(tables),13)
        self.assertEqual(self.tool.REQUIRED_ACCESS["role_application_history"],"SELECT INSERT")
        self.assertIn("LEFT JOIN LATERAL",self.tool.QUERIES["default_acl"])
        self.assertIn("explicitly_empty_acl",self.tool.QUERIES["default_acl"])
        for name in ("table_acl","schema_acl","sequence_acl","function_acl"):
            with self.subTest(section=name):self.assertIn("LEFT JOIN LATERAL",self.tool.QUERIES[name])
        self.assertIn("has_column_privilege",self.tool.QUERIES["view_column_privileges"])
        self.assertIn("aclexplode",self.tool.QUERIES["view_column_acl"])

    def test_history_guard_metadata_must_match_expected_events_and_function(self):
        sections=self.empty_sections()
        sections["tables"]["rows"]=[dict(table_name="role_application_history",schema_name="public",owner="migration_owner",connection_has_owner_privileges=False,rls_enabled=True)]
        def guard(name,bits):return dict(schema_name="public",table_name="role_application_history",trigger_name=name,type_bits=bits,enabled_mode="O",function_schema="public",function_name="prevent_role_application_history_mutation",security_definer=False,internal=False,has_when_condition=False)
        sections["triggers"]["rows"]=[guard("role_application_history_no_mutation",27),guard("role_application_history_no_truncate",34)]
        findings,_=self.tool.assess(sections,self.identity(),"public")
        self.assertFalse(any(f["code"]=="AUDIT_GUARD_METADATA_MISSING" for f in findings))
        sections["triggers"]["rows"][0]["type_bits"]=23
        findings,_=self.tool.assess(sections,self.identity(),"public")
        self.assertTrue(any(f["code"]=="AUDIT_GUARD_METADATA_MISSING" for f in findings))

    def test_nonowner_history_runtime_policy_gap_is_visible(self):
        sections=self.empty_sections()
        sections["effective_table_privileges"]["rows"]=[dict(schema_name="public",table_name="role_application_history",role_name="flask_sql_login",privilege=op,
            schema_usage=True,table_granted=True,any_column_granted=True,rls_restricts_operation=True,applicable_permissive_policy=False) for op in ("SELECT","INSERT")]
        findings,compatibility=self.tool.assess(sections,self.identity(),"public")
        blocked=[f for f in findings if f["code"]=="RUNTIME_OPERATION_BLOCKED" and f["table"]=="role_application_history"]
        self.assertEqual({f["operation"] for f in blocked},{"SELECT","INSERT"})
        self.assertTrue(all(f["severity"]=="HIGH" for f in blocked))

    def test_non_origin_trigger_mode_is_flagged(self):
        identity=self.identity();identity["replication_role"]="replica"
        findings,_=self.tool.assess(self.empty_sections(),identity,"public")
        self.assertTrue(any(f["code"]=="REPLICA_TRIGGER_MODE" for f in findings))

    def test_local_mode_does_not_falsely_claim_origin_triggers_are_disabled(self):
        identity=self.identity();identity["replication_role"]="local"
        findings,_=self.tool.assess(self.empty_sections(),identity,"public")
        self.assertFalse(any(f["code"]=="REPLICA_TRIGGER_MODE" for f in findings))


class MigrationSourceTests(unittest.TestCase):
    def test_graph_has_unique_ids_all_parents_no_cycles_and_one_head(self):
        revisions={}
        for path in (ROOT/"migrations/versions").glob("*.py"):
            values={}
            for node in ast.parse(path.read_text(encoding="utf-8")).body:
                if isinstance(node,ast.Assign):
                    for name in node.targets:
                        if isinstance(name,ast.Name) and name.id in ("revision","down_revision","depends_on"):values[name.id]=ast.literal_eval(node.value)
            rev=values["revision"]
            self.assertNotIn(rev,revisions)
            self.assertTrue(path.name.startswith(rev))
            self.assertIsNone(values.get("depends_on"))
            parent=values["down_revision"]
            revisions[rev]=parent if isinstance(parent,tuple) else (() if parent is None else (parent,))
        self.assertEqual(len(revisions),16)
        parents={p for values in revisions.values() for p in values}
        self.assertLessEqual(parents,set(revisions))
        self.assertEqual(set(revisions)-parents,{"c83d9f2a71e4"})
        self.assertEqual(revisions["a6d2f9c83b10"],("f4c2a8d91e70",))
        self.assertEqual({rev for rev,parents in revisions.items() if not parents},{"0720c7d1e4ca"})
        visited=set();active=set()
        def visit(rev):
            self.assertNotIn(rev,active)
            if rev in visited:return
            active.add(rev)
            for parent in revisions[rev]:visit(parent)
            active.remove(rev);visited.add(rev)
        visit("c83d9f2a71e4")
        self.assertEqual(visited,set(revisions))

    def test_new_migration_is_additive_and_downgrade_refuses_data_loss(self):
        source=(ROOT/"migrations/versions/a6d2f9c83b10_provider_applications_and_history.py").read_text(encoding="utf-8")
        tree=ast.parse(source)
        upgrade=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=="upgrade")
        downgrade=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=="downgrade")
        self.assertIsInstance(downgrade.body[0],ast.Raise)
        for call in ast.walk(upgrade):
            if isinstance(call,ast.Call) and isinstance(call.func,ast.Attribute):self.assertNotIn(call.func.attr,("drop_table","drop_column","drop_constraint","drop_index"))
        for value in ("ENABLE ROW LEVEL SECURITY","role_application_history_no_mutation","role_application_history_no_truncate","role_application_history_id_seq","REVOKE ALL"):
            self.assertIn(value,source)
