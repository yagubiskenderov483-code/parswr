"""Проверка формата карточки: python3 test_tracker_format.py"""

from datetime import datetime, timedelta, timezone

from market import Lot
from tracker import Config, format_lot

cfg = Config(
    api_id=1,
    api_hash="x",
    session_string="",
    bot_token="",
    target_channel="@test",
)

lot = Lot(
    id="1",
    title="Snoop Dogg",
    number=264094,
    stars=800.0,
    slug="SnoopDogg-264094",
    model="Long Beach",
    seller="stichpermskiy",
    seller_id=8266964603,
    is_premium=False,
    free_dm=True,
    account_level=3,
)

ts = datetime(2026, 8, 25, 15, 53, 9, tzinfo=timezone(timedelta(hours=3))).timestamp()
text = format_lot(lot, cfg, ts=ts)

expected = """🎉 <b>НОВЫЙ ЛИСТИНГ</b>

🎁 Гифт: <b>Snoop Dogg</b>
💲 Цена: <b>800 Stars / 8.16 TON</b>
🏷 Модель: <b>Long Beach</b>
👤 Продавец: @stichpermskiy (<code>8266964603</code>)
📶 Level: 3
📢 Сообщения: бесплатно
🕺 Статус: без Premium
🔗 <a href="https://t.me/nft/SnoopDogg-264094">SnoopDogg-264094</a>
🕒 25.08.2026 15:53:09"""

print(text)
print()
assert text == expected, "Формат карточки не совпал с образцом!"

# лот без username продавца и без данных о ЛС/Premium
lot2 = Lot(id="2", title="Lol Pop", number=7, stars=505.0, slug="LolPop-7", seller_id=42)
text2 = format_lot(lot2, cfg, ts=ts)
assert "👤 Продавец: <code>42</code>" in text2
assert "📢 Сообщения: —" in text2
assert "🕺 Статус: —" in text2

print("OK: формат карточки совпадает с образцом")
