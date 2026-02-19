from app.actions import ActionExecutor
from app.models import Decision, MessageEnvelope, NormalizedMessage


def test_publish_message_contains_source_and_region() -> None:
    message = ActionExecutor.format_publish_message(
        raw_text="Toshkentdan Andijonga taxi kerak 2 odam",
        source_link="https://t.me/testgroup/123",
        region_tag="#AndijonViloyati",
    )
    assert "Taxi buyurtma" in message
    assert "#AndijonViloyati" in message
    assert "Status: Yangi" in message
    assert "https://t.me/testgroup/123" in message


def test_publish_message_fallback_source() -> None:
    message = ActionExecutor.format_publish_message(
        raw_text="Namanganga yuradigan moshin bormi",
        source_link="",
        region_tag=None,
    )
    assert "#Uzbekiston" in message
    assert "Status: Yangi" in message
    assert "Manba: private chat" in message


def test_resolve_forward_target_numeric_chat_id() -> None:
    assert ActionExecutor._resolve_forward_target("-1001234567890") == -1001234567890


def test_resolve_forward_target_username() -> None:
    assert ActionExecutor._resolve_forward_target("@taxi_orders_uz") == "@taxi_orders_uz"


def test_publish_message_custom_status() -> None:
    message = ActionExecutor.format_publish_message(
        raw_text="Jartepadan shaharga 1 kishi bor",
        source_link="https://t.me/testgroup/444",
        region_tag="#SamarqandViloyati",
        status_label="Yangilandi",
    )
    assert "Status: Yangilandi" in message


def test_publish_message_contains_sender_profile_link() -> None:
    message = ActionExecutor.format_publish_message(
        raw_text="Toshkentdan Samarqandga 1 kishi bor",
        source_link="https://t.me/testgroup/445",
        region_tag="#SamarqandViloyati",
        sender_profile_link="https://t.me/user?id=123456789",
    )
    assert "Aloqa: https://t.me/user?id=123456789" in message


def test_build_sender_profile_link_from_sender_id() -> None:
    assert ActionExecutor._build_sender_profile_link(123456789) == "https://t.me/user?id=123456789"
    assert ActionExecutor._build_sender_profile_link(None) == ""
    assert ActionExecutor._build_sender_profile_link(-100123) == ""


def test_execute_skips_rate_limit_when_limits_set_to_zero() -> None:
    class _Cooldown:
        def __init__(self) -> None:
            self.action_calls = 0
            self.global_calls = 0

        async def allow_action(self, chat_id: int, action: str, limit: int, window: int) -> bool:
            self.action_calls += 1
            return False

        async def allow_global(self, action: str, limit: int, window: int) -> bool:
            self.global_calls += 1
            return False

    class _Repo:
        async def insert_action(self, chat_id: int, message_id: int, action: str, status: str) -> None:
            return None

    class _BotPublisher:
        def __init__(self) -> None:
            self.sent = 0

        async def send_message(self, chat_id: str | int, text: str) -> int:
            self.sent += 1
            return 101

        async def edit_message(self, chat_id: str | int, message_id: int, text: str) -> None:
            return None

        async def broadcast_to_subscribers(self, text: str) -> tuple[int, int]:
            return (0, 0)

    class _RuntimeSnapshot:
        per_group_actions_hour = 0
        global_actions_minute = 0
        forward_target = "@target_group"
        min_human_delay_sec = 0.3
        max_human_delay_sec = 0.9

    class _RuntimeConfig:
        def snapshot(self) -> _RuntimeSnapshot:
            return _RuntimeSnapshot()

    class _Client:
        async def send_message(self, entity: str | int, message: str, link_preview: bool = False) -> object:
            class _Msg:
                id = 999

            return _Msg()

    import asyncio
    from app.config import Settings

    async def run() -> None:
        settings = Settings(api_id=1, api_hash="hash")
        cooldown = _Cooldown()
        bot_publisher = _BotPublisher()
        executor = ActionExecutor(
            client=_Client(),  # type: ignore[arg-type]
            settings=settings,
            cooldown=cooldown,  # type: ignore[arg-type]
            repository=_Repo(),  # type: ignore[arg-type]
            runtime_config=_RuntimeConfig(),  # type: ignore[arg-type]
            bot_publisher=bot_publisher,  # type: ignore[arg-type]
        )
        msg = NormalizedMessage(
            envelope=MessageEnvelope(
                chat_id=-100100,
                message_id=55,
                sender_id=1,
                raw_text="Toshkentdan Samarqandga 1 kishi bor 998901234567",
                chat_username="source_group",
                chat_title="Source",
            ),
            normalized_text="toshkentdan samarqandga 1 kishi bor 998901234567",
        )
        decision = Decision(should_forward=True, should_reply=False, reason="taxi_order", region_tag="#Uzbekiston")
        await executor.execute(msg, decision)
        assert cooldown.action_calls == 0
        assert cooldown.global_calls == 0
        assert bot_publisher.sent == 1

    asyncio.run(run())
