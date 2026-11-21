"""Optional Discord linking. Learn2Play remains the sign-in authority."""

import time

from flask import Blueprint, abort, current_app, jsonify, redirect, request, session, url_for
from flask_login import current_user, login_required
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

if __package__:
    from . import discord_oauth as oauth
    from .extensions import db
    from .models import DiscordAccount, User
else:
    import discord_oauth as oauth
    from extensions import db
    from models import DiscordAccount, User

discord = Blueprint("discord_accounts", __name__, url_prefix="/api/me/discord")


@discord.before_request
def require_enabled():
    if not current_app.config["DISCORD_LINKING_ENABLED"]:
        abort(404)
    if current_user.is_authenticated and not inspect(db.engine).has_table("discord_accounts"):
        return jsonify(ok=False, message="Discord account storage is unavailable."), 503


@discord.after_request
def protect_response(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@discord.post("/connect")
@login_required
def connect():
    try:
        settings = oauth.get_oauth_settings(current_app.config)
    except oauth.DiscordConfigurationError:
        return jsonify(ok=False, message="Discord linking is unavailable."), 503
    state, session[oauth.SESSION_KEY] = oauth.new_state_record(current_user.id)
    return redirect(oauth.build_authorize_url(settings, state), code=303)


@discord.get("/callback")
@login_required
def callback():
    record = session.pop(oauth.SESSION_KEY, None)
    code = request.args.get("code", "")
    if (
        not record or record["user_id"] != current_user.id or record["expires_at"] <= time.time()
        or not oauth.state_matches(record["state"], request.args.get("state"))
        or request.args.get("error") or not 0 < len(code) <= oauth.MAX_CODE_LENGTH
        or len(request.args.getlist("code")) != 1 or len(request.args.getlist("state")) != 1
    ):
        return jsonify(ok=False, message="Discord authorization expired, was denied, or was invalid."), 400
    user_id = current_user.id
    db.session.rollback()
    try:
        settings = oauth.get_oauth_settings(current_app.config)
        identity = oauth.fetch_identity(settings, code)
        discord_id = oauth.validate_identity(identity)
    except (oauth.DiscordConfigurationError, oauth.DiscordOAuthError):
        return jsonify(ok=False, message="Discord authorization could not be completed."), 503
    user = db.session.get(User, user_id, populate_existing=True, with_for_update=True)
    if user is None or not user.is_active:
        return jsonify(ok=False, message="This Learn2Play account is unavailable."), 403
    account = db.session.get(DiscordAccount, user_id)
    if account is not None:
        if account.discord_id != discord_id:
            return jsonify(ok=False, message="Disconnect the current Discord account before linking another."), 409
    else:
        db.session.add(DiscordAccount(user_id=user_id, discord_id=discord_id))
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return jsonify(ok=False, message="This Discord identity or Learn2Play account is already linked."), 409
    return redirect(url_for("main.settings", _anchor="roles"))


@discord.get("")
@login_required
def status():
    return jsonify(ok=True, linked=db.session.get(DiscordAccount, current_user.id) is not None)


@discord.get("/guild-status")
@login_required
def guild_status():
    account = db.session.get(DiscordAccount, current_user.id)
    if account is None:
        return jsonify(ok=True, status="NOT_LINKED", is_member=None)
    discord_id = account.discord_id
    db.session.rollback()
    try:
        settings = oauth.get_bot_settings(current_app.config)
    except oauth.DiscordConfigurationError:
        return jsonify(ok=True, status="BOT_CONFIG_ERROR", is_member=None)
    return jsonify(ok=True, **oauth.check_guild_membership(settings, discord_id))


@discord.post("/disconnect")
@login_required
def disconnect():
    session.pop(oauth.SESSION_KEY, None)
    DiscordAccount.query.filter_by(user_id=current_user.id).delete()
    db.session.commit()
    return jsonify(ok=True, linked=False)
