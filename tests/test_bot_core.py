"""
Automated test suite for deployMessageBot (Phase 1 + Phase 2 verification).
Run: pytest -v
"""
import os
import sys
import tempfile
import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

# Isolate database BEFORE importing project modules
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_db.name}"
os.environ["BOT_TOKEN"] = "123456:TEST_TOKEN"
os.environ["ADMIN_IDS"] = "999999"
os.environ["BOT_USERNAME"] = "TestDeployBot"
os.environ["INVITE_ONLY"] = "true"
os.environ["SCHEDULER_TIMEZONE"] = "Asia/Riyadh"

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config
from database.models import init_db, get_session, MediaType, DestructMode, MessageStatus, utcnow_naive
from database import db
from bot.services.message_sender import _partition_media_batches, send_configured_message
from bot.services.broadcast import broadcast, send_paid_content_to_user, delete_expired_messages
from bot.services import scheduler as sched
from bot.handlers import user as user_h
from bot.handlers import payment as pay_h
from telegram.error import Forbidden, RetryAfter, TimedOut


@pytest.fixture(scope="module", autouse=True)
def setup_db():
    init_db()
    yield
    try:
        os.unlink(_tmp_db.name)
    except OSError:
        pass


def _make_bot():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=101))
    bot.send_photo = AsyncMock(return_value=SimpleNamespace(message_id=102))
    bot.send_voice = AsyncMock(return_value=SimpleNamespace(message_id=103))
    bot.send_media_group = AsyncMock(return_value=[
        SimpleNamespace(message_id=201),
        SimpleNamespace(message_id=202),
    ])
    bot.send_invoice = AsyncMock()
    bot.delete_message = AsyncMock()
    return bot


# 1. Schema & DB integrity tests
def test_start_msg_ids_and_purchase_fk_safe():
    with get_session() as s:
        u, _ = db.get_or_create_user(s, telegram_id=10001, full_name="Alice")
        db.save_last_start_msg_ids(s, 10001, [11, 22, 33])
        assert db.get_last_start_msg_ids(s, 10001) == [11, 22, 33]

        # Must succeed with SQLite foreign_keys=ON (no -1 FK violation)
        p = db.record_purchase(s, u.id, None, stars_paid=50, charge_id="ch_1", purchase_type="start_msg")
        assert p.message_id is None
        assert p.purchase_type == "start_msg"
        assert db.user_has_purchased(s, u.id, purchase_type="start_msg") is True
        assert db.get_total_stars_earned(s) >= 50


# 2. INVITE_ONLY gate & anti-inflation referral test
@pytest.mark.asyncio
async def test_invite_only_blocks_uninvited_and_counts_new_once():
    bot = _make_bot()
    context = SimpleNamespace(bot=bot, args=[])

    # Uninvited new user -> rejected
    msg = MagicMock()
    msg.reply_text = AsyncMock()
    upd = SimpleNamespace(
        effective_user=SimpleNamespace(id=20001, username="stranger", first_name="Str<X>", full_name="Str<X>"),
        effective_chat=SimpleNamespace(id=20001),
        message=msg,
    )
    await user_h.start(upd, context)
    with get_session() as s:
        assert db.get_user_by_telegram_id(s, 20001) is None

    # Create valid invite link owned by admin
    with get_session() as s:
        link = db.create_invite_link(s, owner_id=999999)
        token = link.token

    # Join with valid invite link -> accepted, uses = 1
    context.args = [f"ref_{token}"]
    await user_h.start(upd, context)
    # Clicking invite link again as existing user -> must NOT increment uses
    await user_h.start(upd, context)

    with get_session() as s:
        u = db.get_user_by_telegram_id(s, 20001)
        assert u is not None and u.referred_by == 999999
        refreshed = db.get_invite_link_by_token(s, token)
        assert refreshed.uses == 1


# 3. Paid broadcast zero-leak & Forbidden deactivation
@pytest.mark.asyncio
async def test_paid_broadcast_no_content_leak_and_forbidden_deactivates():
    with get_session() as s:
        u_ok, _ = db.get_or_create_user(s, telegram_id=30001, full_name="Buyer")
        u_blk, _ = db.get_or_create_user(s, telegram_id=30002, full_name="Blocked")
        msg = db.create_broadcast_message(
            s,
            title="VIP Drop",
            text="SECRET_PAID_CONTENT_XYZ",
            media_type=MediaType.PHOTO,
            media_file_id="secret_photo_fid",
            is_paid=True,
            stars_price=25,
            protect_content=True,
            destruct_mode=DestructMode.NEVER,
            destruct_seconds=0,
            target="users",
            status=MessageStatus.DRAFT,
            created_by=999999,
            buttons_json='[{"text":"SecretLink","url":"https://secret.example.com"}]',
            media_json='[{"type":"photo","file_id":"secret_photo_fid"}]',
        )
        mid = msg.id

    bot = _make_bot()

    async def fake_send_message(**kwargs):
        if kwargs["chat_id"] == 30002:
            raise Forbidden("Bot was blocked by the user")
        return SimpleNamespace(message_id=501)

    bot.send_message = AsyncMock(side_effect=fake_send_message)

    stats = await broadcast(bot, mid, target="users")
    assert stats["blocked_users"] >= 1
    # Ensure secret photo was NEVER sent before payment
    bot.send_photo.assert_not_awaited()
    # Ensure secret text was NEVER sent in teaser
    for call in bot.send_message.await_args_list:
        assert "SECRET_PAID_CONTENT_XYZ" not in call.kwargs.get("text", "")

    with get_session() as s:
        assert db.get_user_by_telegram_id(s, 30002).is_active is False


# 4. Pre-checkout validation (price mismatch & banned)
@pytest.mark.asyncio
async def test_pre_checkout_rejects_banned_and_price_mismatch():
    with get_session() as s:
        db.get_or_create_user(s, telegram_id=40001, full_name="BannedUser")
        db.ban_user(s, 40001)
        db.get_or_create_user(s, telegram_id=40002, full_name="GoodUser")
        m = db.create_broadcast_message(
            s, text="Paid", is_paid=True, stars_price=100,
            created_by=999999, status=MessageStatus.SENT,
        )
        mid = m.id

    # Banned user rejected
    q1 = SimpleNamespace(
        currency="XTR", invoice_payload=f"paid_msg_{mid}",
        total_amount=100, from_user=SimpleNamespace(id=40001),
        answer=AsyncMock(),
    )
    await pay_h.pre_checkout(SimpleNamespace(pre_checkout_query=q1), SimpleNamespace())
    assert q1.answer.await_args.kwargs["ok"] is False

    # Wrong amount rejected
    q2 = SimpleNamespace(
        currency="XTR", invoice_payload=f"paid_msg_{mid}",
        total_amount=10, from_user=SimpleNamespace(id=40002),
        answer=AsyncMock(),
    )
    await pay_h.pre_checkout(SimpleNamespace(pre_checkout_query=q2), SimpleNamespace())
    assert q2.answer.await_args.kwargs["ok"] is False

    # Valid purchase approved
    q3 = SimpleNamespace(
        currency="XTR", invoice_payload=f"paid_msg_{mid}",
        total_amount=100, from_user=SimpleNamespace(id=40002),
        answer=AsyncMock(),
    )
    await pay_h.pre_checkout(SimpleNamespace(pre_checkout_query=q3), SimpleNamespace())
    assert q3.answer.await_args.kwargs["ok"] is True


# 5. Media batch partitioning & voice separation
def test_media_partition_compliance():
    items = (
        [{"type": "photo", "file_id": f"p{i}"} for i in range(12)]
        + [{"type": "document", "file_id": "d1"}]
        + [{"type": "voice", "file_id": "v1"}]
        + [{"type": "video", "file_id": "vid1"}]
    )
    batches = _partition_media_batches(items)
    assert [len(b) for b in batches] == [10, 2, 1, 1, 1]
    assert batches[2][0]["type"] == "document"
    assert batches[3][0]["type"] == "voice"


# 6. Transient delete error must NOT mark SelfDestructJob done
@pytest.mark.asyncio
async def test_cleanup_preserves_job_on_timeout():
    with get_session() as s:
        past = utcnow_naive() - datetime.timedelta(seconds=60)
        from database.models import SelfDestructJob
        job = SelfDestructJob(chat_id=55555, message_id=777, delete_at=past, deleted=False)
        s.add(job)
        s.commit()
        jid = job.id

    bot = _make_bot()
    bot.delete_message = AsyncMock(side_effect=TimedOut("network timeout"))
    await delete_expired_messages(bot)

    with get_session() as s:
        from database.models import SelfDestructJob
        row = s.query(SelfDestructJob).filter_by(id=jid).first()
        assert row.deleted is False  # kept for retry


# 7. Batch deactivation functions exist and work
def test_batch_deactivation():
    with get_session() as s:
        db.get_or_create_user(s, telegram_id=70001, full_name="Block1")
        db.get_or_create_user(s, telegram_id=70002, full_name="Block2")
        count = db.deactivate_users_batch(s, [70001, 70002])
        assert count == 2
        u1 = db.get_user_by_telegram_id(s, 70001)
        assert u1.is_active is False


# 8. get_message_stats returns correct dict keys
def test_get_message_stats():
    with get_session() as s:
        stats = db.get_message_stats(s)
        assert "sent" in stats
        assert "scheduled" in stats
        assert "draft" in stats
        assert "cancelled" in stats


# 9. Pagination works
def test_messages_paginated():
    with get_session() as s:
        items, total = db.get_messages_paginated(s, page=0, per_page=5)
        assert isinstance(items, list)
        assert isinstance(total, int)
        assert total >= 0
