"""Private appointment voice channels. Never retry an ambiguous create."""

import secrets
from datetime import timedelta

from flask import Blueprint, abort, current_app, jsonify, request
from flask_login import current_user, login_required
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

if __package__:
    from . import discord_oauth as oauth
    from .extensions import db
    from .models import Appointment, DiscordAccount, DiscordRoom, DiscordRoomEvent, utcnow
    from .routes import admin_required, provider_approved_for_role
    from .scheduling import lock_schedules, reservation_errors
else:
    import discord_oauth as oauth
    from extensions import db
    from models import Appointment, DiscordAccount, DiscordRoom, DiscordRoomEvent, utcnow
    from routes import admin_required, provider_approved_for_role
    from scheduling import lock_schedules, reservation_errors


rooms = Blueprint("discord_rooms", __name__, url_prefix="/api")


@rooms.before_request
def require_config():
    if not current_app.config["DISCORD_SESSIONS_ENABLED"]:
        abort(404)
    if current_user.is_authenticated and not inspect(db.engine).has_table("discord_rooms"):
        return failure("Discord room storage requires a database upgrade.", 503)
    return None


@rooms.after_request
def prevent_cache(response):
    response.headers["Cache-Control"] = "no-store"
    return response


def failure(message, status, retry_after=None):
    response = jsonify(ok=False, message=message)
    if retry_after is not None:
        response.headers["Retry-After"] = str(int(retry_after) + 1)
    return response, status


def eligible(appointment):
    return (
        appointment.status in ("accepted", "scheduled")
        and appointment.start_time + timedelta(minutes=appointment.duration_minutes) > utcnow()
        and provider_approved_for_role(appointment.provider, appointment.role)
        and appointment.student.is_active
        and not appointment.attendance_disputed
        and appointment.payment_satisfied
        and (not appointment.payment_required or not current_app.config["PAYMENT_DEMO"])
    )


def room_response(room):
    return jsonify(ok=True, url=f"https://discord.com/channels/{room.guild_id}/{room.channel_id}")


def audit(room, actor_id, action):
    db.session.add(DiscordRoomEvent(appointment_id=room.appointment_id, actor_id=actor_id, action=action))


@rooms.post("/sessions/<int:booking_id>/discord-channel")
@login_required
@reservation_errors
def provision(booking_id):
    actor_id = current_user.id
    appointment = db.session.get(Appointment, booking_id)
    if appointment is None or actor_id not in (appointment.student_id, appointment.provider_id):
        abort(404)
    lock_schedules(appointment.student_id, appointment.provider_id)
    appointment = db.session.get(Appointment, booking_id, populate_existing=True, with_for_update=True)
    if not eligible(appointment):
        return failure("This appointment is not eligible for a Discord voice room.", 403)
    student = db.session.get(DiscordAccount, appointment.student_id)
    provider = db.session.get(DiscordAccount, appointment.provider_id)
    if student is None or provider is None:
        return failure("Both participants must link Discord first.", 409)
    try:
        settings = oauth.get_bot_settings(current_app.config)
        identities = (student.discord_id, provider.discord_id)
        oauth.build_private_voice_overwrites(settings["guild_id"], *identities)
    except (oauth.DiscordConfigurationError, oauth.DiscordChannelValidationError):
        return failure("Discord room configuration is unavailable.", 503)

    room = db.session.get(DiscordRoom, booking_id, populate_existing=True, with_for_update=True)
    if room and (room.guild_id != settings["guild_id"]
                 or identities != (room.student_discord_id, room.provider_discord_id)):
        return failure("This room needs administrator review.", 409)
    if room and room.state == "ready":
        return room_response(room)
    if room and room.state != "failed":
        return failure("An existing room request needs administrator review.", 409)
    marker = "learn2play-" + secrets.token_hex(16)
    if room:
        claimed = DiscordRoom.query.filter_by(appointment_id=booking_id, state="failed").update(
            {"state": "provisioning", "marker": marker}, synchronize_session=False)
        if not claimed:
            return failure("A room request is already in progress.", 409)
    else:
        db.session.add(DiscordRoom(appointment_id=booking_id, guild_id=settings["guild_id"],
                                   student_discord_id=identities[0], provider_discord_id=identities[1],
                                   marker=marker, state="provisioning"))
        try:
            db.session.flush()
        except IntegrityError:
            return failure("A room request is already in progress.", 409)
    db.session.commit()

    for identity in identities:
        membership = oauth.check_guild_membership(settings, identity)
        if membership["is_member"] is not True:
            DiscordRoom.query.filter_by(appointment_id=booking_id, marker=marker, state="provisioning").update(
                {"state": "failed"}, synchronize_session=False)
            db.session.commit()
            return failure("Both participants must be confirmed members of the Discord server.", 409,
                           membership.get("retry_after"))
    result = oauth.create_private_voice_channel(settings, marker, *identities)
    room = db.session.get(DiscordRoom, booking_id, populate_existing=True, with_for_update=True)
    if room.marker != marker or room.state != "provisioning":
        return failure("This room is under administrator review.", 409)
    if result["ok"]:
        room.channel_id = result["channel_id"]
        room.state = "ready"
    elif not result["outcome_unknown"]:
        room.state = "failed"
    audit(room, actor_id, room.state)
    db.session.commit()
    if not result["ok"]:
        return failure("Discord could not confirm room creation.", 503, result.get("retry_after"))
    appointment = db.session.get(Appointment, booking_id, populate_existing=True)
    if not eligible(appointment):
        return failure("The appointment changed while the room was being created.", 409)
    return room_response(room)


@rooms.get("/admin/discord/rooms/<int:booking_id>")
@admin_required
def room_status(booking_id):
    room = db.session.get(DiscordRoom, booking_id)
    if room is None:
        abort(404)
    return jsonify(ok=True, state=room.state, channel_id=room.channel_id, marker=room.marker,
                   events=[{"action": e.action, "actor_id": e.actor_id, "at": e.created_at.isoformat()}
                           for e in DiscordRoomEvent.query.filter_by(appointment_id=booking_id)
                           .order_by(DiscordRoomEvent.id)])


@rooms.post("/admin/discord/rooms/<int:booking_id>/<action>")
@admin_required
@reservation_errors
def manage_room(booking_id, action):
    if action not in ("recover", "cleanup"):
        abort(404)
    lock_schedules()
    room = db.session.get(DiscordRoom, booking_id, populate_existing=True, with_for_update=True)
    if room is None:
        abort(404)
    if action == "cleanup" and room.state == "removed":
        return jsonify(ok=True, state=room.state)
    data = request.get_json(silent=True)
    channel_id = data.get("channel_id") if action == "recover" and isinstance(data, dict) else room.channel_id
    if room.state != ("provisioning" if action == "recover" else "ready") or not oauth._is_snowflake(channel_id):
        return failure("This room cannot be managed in its current state.", 409)
    try:
        settings = oauth.get_bot_settings(current_app.config)
        if settings["guild_id"] != room.guild_id:
            return failure("The configured Discord server differs from the room record.", 409)
        result = oauth._bot_api_request("GET", oauth.DISCORD_CHANNEL_URL.format(channel_id=channel_id),
                                        settings["bot_token"])
    except (oauth.DiscordOAuthError, oauth.DiscordConfigurationError):
        return failure("Discord could not verify this channel.", 503)
    payload = result["payload"] or {}
    if action == "cleanup" and result["status"] == 404 and payload.get("code") == 10003:
        room.state = "removed"
    else:
        expected = oauth.build_private_voice_overwrites(room.guild_id, room.student_discord_id,
                                                       room.provider_discord_id)
        if (result["status"] != 200 or payload.get("id") != channel_id or payload.get("name") != room.marker
                or not oauth._channel_matches(payload, room.guild_id, expected)):
            return failure("Discord could not prove that this is the Learn2Play room.", 409,
                           result.get("retry_after"))
        if action == "recover":
            room.channel_id = channel_id
            room.state = "ready"
        else:
            deleted = oauth.delete_voice_channel(settings, channel_id)
            if not deleted["ok"]:
                return failure("Discord did not confirm room cleanup.", 503, deleted.get("retry_after"))
            room.state = "removed"
    audit(room, current_user.id, "recovered" if action == "recover" else "removed")
    db.session.commit()
    return jsonify(ok=True, state=room.state)
