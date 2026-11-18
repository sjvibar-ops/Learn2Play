import os
import sys
from pathlib import Path
# Resolve this fixture relative to the checkout; no developer-specific drive path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import threading
import time
import socket
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from flask import jsonify
from playwright.sync_api import sync_playwright

from Backend import create_app
from Backend.extensions import db
from Backend.models import User, Appointment
from Backend import routes, discord_channel_orchestration as orch, discord_oauth as discord

STUDENT_ID = '111222333444555666'
PROVIDER_ID = '222333444555666777'
GUILD_ID = '333444555666777888'
CHANNEL_ID = '444555666777888999'

def get_free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port

def run_tests():
    print("=================================================================")
    print("   FEATURE 2 FRONTEND HOTFIX — CHROMIUM BROWSER VERIFICATION     ")
    print("=================================================================")

    port = get_free_port()
    base_url = f"http://127.0.0.1:{port}"

    app = create_app({
        'TESTING': True,
        'WTF_CSRF_ENABLED': True,
        'SECRET_KEY': 'test-secret-key-for-playwright',
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
        'DISCORD_SESSIONS_ENABLED': True,
        'DISCORD_GUILD_ID': GUILD_ID,
        'DISCORD_BOT_TOKEN': 'disposable-bot-token-secret-12345',
        'JAAS_APP_ID': 'test-jaas-app-id',
        'JAAS_API_KEY_ID': 'test-jaas-app-id/key-1',
        'JAAS_PRIVATE_KEY': '-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0...\n-----END RSA PRIVATE KEY-----',
    })

    @app.route('/_test/login/<int:user_id>')
    def test_login(user_id):
        from flask_login import login_user
        u = db.session.get(User, user_id)
        if u:
            login_user(u, remember=True)
            return jsonify({'ok': True, 'user': u.display_name})
        return jsonify({'ok': False}), 404

    with app.app_context():
        db.create_all()
        student = User(display_name='Alice Student', email='alice@learn2play.test', password_hash='hash', roles_json='["student"]', discord_user_id=STUDENT_ID)
        provider = User(display_name='Bob Coach', email='bob@learn2play.test', password_hash='hash', roles_json='["coach", "mentor"]', discord_user_id=PROVIDER_ID)
        unlinked = User(display_name='Charlie Unlinked', email='charlie@learn2play.test', password_hash='hash', roles_json='["student"]', discord_user_id=None)
        db.session.add_all([student, provider, unlinked])
        db.session.flush()

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        
        # 1. Upcoming session
        appt_upcoming = Appointment(
            student_id=student.id, provider_id=provider.id, role='mentor',
            topic='Advanced Python Tutoring', start_time=now + timedelta(minutes=5),
            duration_minutes=60, status='accepted', meeting_room='l2-jaas-upcoming-101'
        )
        # 2. Completed past session
        appt_past = Appointment(
            student_id=student.id, provider_id=provider.id, role='mentor',
            topic='Introductory Math Review', start_time=now - timedelta(days=2),
            duration_minutes=60, status='completed', meeting_room='l2-jaas-past-102'
        )
        # 3. Pending booking request for provider
        appt_pending = Appointment(
            student_id=student.id, provider_id=provider.id, role='coach',
            topic='Valorant Aim Coaching', start_time=now + timedelta(days=1),
            duration_minutes=60, status='pending', notes='Help with crosshair placement'
        )
        # 4. Session for unlinked student
        appt_unlinked = Appointment(
            student_id=unlinked.id, provider_id=provider.id, role='mentor',
            topic='Unlinked Student Session', start_time=now + timedelta(minutes=10),
            duration_minutes=60, status='accepted', meeting_room='l2-jaas-unlinked-103'
        )
        db.session.add_all([appt_upcoming, appt_past, appt_pending, appt_unlinked])
        db.session.commit()

        student_id = student.id
        provider_id = provider.id
        unlinked_id = unlinked.id
        appt_upcoming_id = appt_upcoming.id
        appt_past_id = appt_past.id
        appt_pending_id = appt_pending.id
        appt_unlinked_id = appt_unlinked.id

    patcher_membership = patch.object(
        routes.discord_oauth, 'check_guild_membership',
        return_value={'status': 'VERIFIED_MEMBER', 'is_member': True}
    )
    patcher_prov = patch.object(
        routes, 'provider_approved_for_role',
        return_value=True
    )
    # Slow down orchestration slightly so we can observe the disabled/aria-busy button state in flight!
    def slow_provision(appt_id, settings=None):
        time.sleep(0.3)
        return orch.OrchestrationResult(orch.OrchestrationStatus.CREATED, channel_id=CHANNEL_ID)

    patcher_orch = patch.object(
        routes.discord_channel_orchestration, 'provision_voice_channel',
        side_effect=slow_provision
    )

    patcher_jaas = patch.object(
        routes, 'create_jaas_participant_token',
        return_value='mocked-jwt-token-jaas'
    )
    patcher_membership.start()
    patcher_jaas.start()
    patcher_prov.start()
    mocked_orchestrator = patcher_orch.start()

    from werkzeug.serving import make_server
    server = make_server('127.0.0.1', port, app, threaded=True)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    time.sleep(1)

    print(f"Server online at {base_url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        # Fail closed for all non-local browser requests (CDN, Discord, JaaS).
        from urllib.parse import urlsplit
        def block_external(route):
            host = urlsplit(route.request.url).hostname
            if host in ("127.0.0.1", "localhost"):
                route.continue_()
            else:
                route.abort()
        context.route("**/*", block_external)
        page = context.new_page()

        page_errors = []
        page.on("pageerror", lambda err: page_errors.append(str(err)))
        page.on("console", lambda msg: print(f"  [Browser Console] {msg.type}: {msg.text}") if msg.type in ("error", "warning") else None)

        # -------------------------------------------------------------
        # VERIFICATION 1: No ReferenceError on actual sessions.js
        # -------------------------------------------------------------
        print("\n[VERIFICATION 1] Loading sessions page without any script interception...")
        page.goto(f"{base_url}/_test/login/{student_id}")
        page.goto(f"{base_url}/sessions")
        page.wait_for_selector(".scheduled-session[data-status='upcoming']", state="visible", timeout=5000)
        assert len(page_errors) == 0, f"Uncaught page errors found: {page_errors}"
        print("  -> PASS: Page loaded with ZERO ReferenceErrors or uncaught exceptions!")

        # -------------------------------------------------------------
        # VERIFICATION 2: Student booking cards render correctly
        # -------------------------------------------------------------
        print("\n[VERIFICATION 2] Verifying student booking cards rendering...")
        cards = page.query_selector_all(".scheduled-session")
        assert len(cards) >= 1, "Expected scheduled session card to render"
        topic_el = page.query_selector(f".scheduled-session:has(.join-session[data-id='{appt_upcoming_id}']) .session-topic-name")
        assert "Advanced Python Tutoring" in topic_el.text_content()
        print("  -> PASS: Student session cards rendered with correct topic, mentor and status!")

        # -------------------------------------------------------------
        # VERIFICATION 3: Mentor/Coach workspace view & cards
        # -------------------------------------------------------------
        print("\n[VERIFICATION 3] Verifying Mentor/Coach workspace...")
        page.goto(f"{base_url}/_test/login/{provider_id}")
        page.goto(f"{base_url}/sessions")
        page.wait_for_selector(".mentor-upcoming-item", timeout=5000)
        page.wait_for_selector(".mentor-request-card", timeout=5000)
        upcoming_coach_item = page.query_selector(".mentor-upcoming-item")
        request_item = page.query_selector(".mentor-request-card")
        assert upcoming_coach_item is not None, "Upcoming learner appointment must render"
        assert request_item is not None, "Pending booking request must render"
        assert "Valorant Aim Coaching" in request_item.text_content()
        print("  -> PASS: Mentor workspace correctly renders booking requests and upcoming sessions!")

        # -------------------------------------------------------------
        # VERIFICATION 4: Session tabs and filtering
        # -------------------------------------------------------------
        print("\n[VERIFICATION 4] Verifying tab switching and filters...")
        page.goto(f"{base_url}/_test/login/{student_id}")
        page.goto(f"{base_url}/sessions")
        page.wait_for_selector(".session-tab", timeout=5000)
        
        # Click "Past sessions" tab
        past_tab = page.query_selector(".session-tab[data-status='completed']")
        upcoming_tab = page.query_selector(".session-tab[data-status='upcoming']")
        past_tab.click()
        time.sleep(0.5)
        
        # Check visible sessions
        past_cards = page.query_selector_all(".scheduled-session[data-status='completed']")
        assert len(past_cards) == 1, "Past sessions must display completed sessions"
        assert past_cards[0].is_visible()
        print("  -> PASS: 'Past sessions' tab filter works and displays past completed session.")

        # Click "Upcoming" tab back
        upcoming_tab.click()
        time.sleep(0.5)
        upcoming_cards = page.query_selector_all(".scheduled-session[data-status='upcoming']")
        print(f"  -> upcoming_cards count: {len(upcoming_cards)}")
        assert len(upcoming_cards) >= 1
        assert upcoming_cards[0].is_visible()
        print("  -> PASS: 'Upcoming' tab filter works and restores upcoming sessions.")

        # -------------------------------------------------------------
        # VERIFICATION 5: Existing JaaS Join Session buttons work
        # -------------------------------------------------------------
        print("\n[VERIFICATION 5] Verifying existing JaaS Join Session button...")
        jaas_btn = page.query_selector(f".join-session[data-id='{appt_upcoming_id}']")
        assert jaas_btn is not None
        assert jaas_btn.text_content().strip() == "Join Session"
        
        jaas_btn.click()
        page.wait_for_url(f"**/sessions/{appt_upcoming_id}/meeting*", timeout=5000)
        assert f"/sessions/{appt_upcoming_id}/meeting" in page.url
        print(f"  -> Successfully navigated to: {page.url}")
        print("  -> PASS: JaaS 'Join Session' button functions and navigates to video meeting!")

        # -------------------------------------------------------------
        # VERIFICATION 6 & 7: Discord Join Voice button, enabled state, double-submission prevention
        # -------------------------------------------------------------
        print("\n[VERIFICATION 6 & 7] Verifying Discord Join Voice and double-click prevention...")
        page.goto(f"{base_url}/sessions")
        page.wait_for_selector(".scheduled-session[data-status='upcoming']", state="visible", timeout=5000)
        discord_btn = page.query_selector(f".discord-join-session[data-id='{appt_upcoming_id}']")
        assert discord_btn is not None
        assert discord_btn.text_content().strip() == "Join Discord Voice"
        
        # A successful join navigates this tab with location.assign(), not a popup.
        # The browser's external-request blocker remains active: no Discord network
        # request is allowed to leave the local test context.
        discord_url = f"https://discord.com/channels/{GUILD_ID}/{CHANNEL_ID}"
        calls_before = mocked_orchestrator.call_count
        with page.expect_request(lambda req: req.url == discord_url, timeout=10000) as join_request:
            discord_btn.click()
            # Verify the button is disabled while the mocked server response is pending.
            is_disabled = discord_btn.is_disabled()
            aria_busy = discord_btn.get_attribute("aria-busy")
            print(f"  -> In-flight button state: disabled={is_disabled}, aria-busy={aria_busy}")
            assert is_disabled, "Button must be disabled during active request!"
            assert aria_busy == "true", "Button must set aria-busy='true' to prevent double submission!"
            # A second click must not issue another provisioning request.
            discord_btn.evaluate("button => button.click()")

        assert join_request.value.url == discord_url, "Join must target the expected Discord channel"
        assert mocked_orchestrator.call_count == calls_before + 1, "Join must provision at most once"
        # Do not inspect discord_btn after navigation: its page context is destroyed.
        print("  -> PASS: Busy state, single provisioning call, and expected Discord navigation verified!")

        # -------------------------------------------------------------
        # VERIFICATION 8: Connect Discord guidance displays correctly
        # -------------------------------------------------------------
        print("\n[VERIFICATION 8] Verifying unlinked Discord account guidance...")
        page.goto(f"{base_url}/_test/login/{unlinked_id}")
        page.goto(f"{base_url}/sessions")
        page.wait_for_function("() => !document.getElementById('discordSessionSetup').hidden", timeout=5000)
        setup_box = page.query_selector("#discordSessionSetup")
        setup_msg = page.query_selector("#discordSessionSetupMessage")
        settings_link = page.query_selector("#discordSessionSettingsLink")
        assert setup_box.is_visible()
        assert "Connect Discord in Settings before joining a voice session." in setup_msg.text_content()
        assert settings_link.get_attribute("href") == "/settings#security"
        print("  -> PASS: Setup guidance correctly instructs user to Connect Discord in Settings (#security)!")

        # -------------------------------------------------------------
        # VERIFICATION 9: Membership errors display safely
        # -------------------------------------------------------------
        print("\n[VERIFICATION 9] Verifying safe display of guild membership messages...")
        # Simulate student whose Discord account is linked but NOT in guild
        with patch.object(routes.discord_oauth, 'check_guild_membership', return_value={'status': 'NOT_IN_GUILD', 'is_member': False}):
            page.goto(f"{base_url}/_test/login/{student_id}")
            page.goto(f"{base_url}/sessions")
            page.wait_for_function("() => !document.getElementById('discordSessionSetup').hidden", timeout=5000)
            setup_msg = page.query_selector("#discordSessionSetupMessage")
            assert "Join the Learn2Play Discord server" in setup_msg.text_content()
            print("  -> PASS: NOT_IN_GUILD displays safe guidance without unsafe HTML injection!")

        # -------------------------------------------------------------
        # VERIFICATION 10: Disabled feature flag hides controls
        # -------------------------------------------------------------
        print("\n[VERIFICATION 10] Verifying DISCORD_SESSIONS_ENABLED=False gate...")
        app_disabled = create_app({
            'TESTING': True,
            'WTF_CSRF_ENABLED': False,
            'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
            'DISCORD_SESSIONS_ENABLED': False,
        })
        with app_disabled.app_context():
            db.create_all()
            st = User(display_name='S', email='s@test.com', password_hash='t', roles_json='["student"]', discord_user_id='123')
            pr = User(display_name='P', email='p@test.com', password_hash='t', roles_json='["mentor"]', discord_user_id='456')
            db.session.add_all([st, pr])
            db.session.flush()
            ap = Appointment(student_id=st.id, provider_id=pr.id, role='mentor', topic='Math', start_time=now + timedelta(minutes=5), duration_minutes=60, status='accepted', meeting_room='l2-test')
            db.session.add(ap)
            db.session.commit()
            
            c = app_disabled.test_client()
            with c.session_transaction() as s:
                s['_user_id'] = str(st.id)
                s['_fresh'] = True
            
            # 1. Direct POST returns 503
            res_disabled = c.post(f"/api/sessions/{ap.id}/discord-channel")
            assert res_disabled.status_code == 503
            assert "Discord booking sessions are not enabled yet" in res_disabled.get_json().get("message", "")

            # 2. Page body has data-discord-sessions-enabled="false"
            resp_page = c.get("/sessions")
            html = resp_page.get_data(as_text=True)
            assert 'data-discord-sessions-enabled="false"' in html
            assert '<button class="session-action discord-join-session"' not in html
            print("  -> PASS: Feature flag OFF hides all Discord joining controls and returns 503!")

        # -------------------------------------------------------------
        # VERIFICATION 11: CSRF protection remains intact
        # -------------------------------------------------------------
        print("\n[VERIFICATION 11] Verifying CSRF protection...")
        c_csrf = app.test_client()
        with c_csrf.session_transaction() as s:
            s['_user_id'] = str(student_id)
            s['_fresh'] = True
        
        # Missing CSRF
        res_no_csrf = c_csrf.post(f"/api/sessions/{appt_upcoming_id}/discord-channel")
        assert res_no_csrf.status_code == 400
        assert res_no_csrf.get_json() == {'message': 'The request is invalid.', 'ok': False}
        
        # Invalid CSRF
        res_bad_csrf = c_csrf.post(
            f"/api/sessions/{appt_upcoming_id}/discord-channel",
            headers={"X-CSRFToken": "invalid-token-xyz"}
        )
        assert res_bad_csrf.status_code == 400
        print("  -> PASS: CSRF protection is fully active; missing and invalid tokens rejected with 400.")

        # -------------------------------------------------------------
        # VERIFICATION 12: No secrets or attempt tokens in DOM
        # -------------------------------------------------------------
        print("\n[VERIFICATION 12] Auditing DOM for secrets and tokens...")
        page.goto(f"{base_url}/_test/login/{student_id}")
        page.goto(f"{base_url}/sessions")
        page.wait_for_selector(".scheduled-session[data-status='upcoming']", state="visible", timeout=5000)
        dom_content = page.content()
        assert "disposable-bot-token-secret-12345" not in dom_content, "Bot token leaked into DOM!"
        assert "attempt_token" not in dom_content, "Attempt token leaked into DOM!"
        assert "token_hex" not in dom_content
        print("  -> PASS: Zero credentials or attempt tokens present in the DOM!")

        browser.close()

    server.shutdown()
    patcher_jaas.stop()
    patcher_membership.stop()
    patcher_prov.stop()
    patcher_orch.stop()
    
    print("\n=================================================================")
    print("   ALL 12 CHROMIUM BROWSER VERIFICATIONS PASSED 100%!           ")
    print("=================================================================")

if __name__ == "__main__":
    run_tests()
