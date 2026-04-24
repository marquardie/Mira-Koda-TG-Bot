from __future__ import annotations

import csv
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
TEXTS_JSON = ROOT / "texts.json"
OUTPUT_CSV = ROOT / "data" / "google_texts_import_with_descriptions.csv"


EXACT_DESCRIPTIONS = {
    "welcome_new": "Приветственное сообщение новому клиенту",
    "welcome_returning": "Приветственное сообщение постоянному клиенту",
    "menu_title": "Заголовок главного меню",
    "rules_text": "Текст правил перед сессией",
    "rules_menu": "Полный текст правил из меню",
    "choose_slot": "Сообщение выбора слота",
    "no_slots": "Сообщение когда нет свободных слотов",
    "confirm_slot": "Подтверждение выбранного слота",
    "booking_cancelled": "Сообщение об отмене выбора слота",
    "booking_reserved": "Сообщение о резерве слота до оплаты",
    "booking_free_via_credit": "Сообщение о брони за счет баланса",
    "slot_released_timeout": "Сообщение о снятии резерва по таймауту",
    "payment_method_prompt": "Сообщение выбора способа оплаты",
    "payment_waiting_admin": "Сообщение пользователю ждать подтверждение оплаты",
    "payment_confirmed_short": "Короткое сообщение об подтвержденной оплате",
    "payment_confirmed": "Полное сообщение об подтвержденной оплате",
    "reminder": "Напоминание о сессии",
    "package_offer": "Сообщение о доступности пакета консультаций",
    "package_offer_card": "Карточка предложения пакета",
    "package_method_prompt": "Сообщение выбора способа оплаты пакета",
    "package_received_pending": "Сообщение о заявке на пакет в ожидании",
    "package_activated": "Сообщение об активации пакета",
    "package_rejected_user": "Сообщение об отклонении оплаты пакета",
    "package_already_active": "Сообщение что пакет уже активен",
    "my_bookings_empty": "Сообщение когда у клиента нет записей",
    "my_bookings_header": "Заголовок раздела мои записи",
    "my_bookings_card": "Карточка одной записи клиента",
    "cancel_reason_prompt": "Запрос причины отмены записи",
    "cancel_reason_saved": "Подтверждение сохранения причины отмены",
    "reschedule_not_allowed": "Сообщение что перенос недоступен",
    "reschedule_choose_slot": "Сообщение выбора нового слота для переноса",
    "reschedule_done": "Сообщение об успешном переносе записи",
    "reschedule_no_slots": "Сообщение когда нет слотов для переноса",
    "back_to_menu": "Сообщение возврата в главное меню",
    "profile_header": "Шапка профиля клиента",
    "profile_bookings_header": "Заголовок списка активных записей в профиле",
    "profile_bookings_line": "Строка активной записи в профиле",
    "profile_bookings_empty": "Сообщение что активных записей нет",
    "help_text": "Текст помощи",
    "admin_help": "Справка по админ-командам",
    "admin_panel_title": "Заголовок админ-панели",
    "admin_only": "Сообщение что команда только для админа",
    "error_generic": "Общая ошибка",
    "cancelled": "Общее сообщение об отмене действия",
    "unknown_command": "Сообщение на неизвестную команду",
    "anketa_in_progress": "Сообщение что анкета еще не завершена",
}


PREFIX_DESCRIPTIONS = [
    ("menu_", "Кнопка или текст главного меню"),
    ("btn_", "Текст кнопки"),
    ("q_", "Вопрос анкеты"),
    ("status_", "Текст статуса записи"),
    ("package_", "Текст, связанный с пакетом консультаций"),
    ("payment_", "Текст, связанный с оплатой"),
    ("method_", "Название способа оплаты"),
    ("cancel_", "Текст, связанный с отменой записи"),
    ("reschedule_", "Текст, связанный с переносом записи"),
    ("slot_", "Текст, связанный со слотами"),
    ("send_", "Текст отправки сообщения"),
    ("bookings_", "Текст списка бронирований"),
    ("admin_menu_", "Кнопка или текст меню админа"),
    ("admin_btn_", "Кнопка админки"),
    ("admin_msg_", "Текст отправки сообщения от админа"),
    ("admin_reply_", "Текст ответа клиенту от админа"),
    ("admin_del_", "Текст удаления в админке"),
    ("admin_cal_", "Текст календаря админа"),
    ("admin_day_", "Текст просмотра дня в админке"),
    ("admin_today_", "Текст раздела сегодня в админке"),
    ("admin_range_", "Текст добавления диапазона слотов"),
    ("admin_client_", "Текст карточки клиента в админке"),
    ("admin_cancel_", "Текст отмены записи админом"),
    ("admin_resch_", "Текст переноса записи админом"),
    ("admin_package_", "Текст оплаты пакета для админа"),
    ("admin_", "Прочий текст админки"),
]


def describe_key(key: str) -> str:
    if key in EXACT_DESCRIPTIONS:
        return EXACT_DESCRIPTIONS[key]
    for prefix, description in PREFIX_DESCRIPTIONS:
        if key.startswith(prefix):
            return description
    return "Системный текст бота"


def main() -> None:
    texts = json.loads(TEXTS_JSON.read_text(encoding="utf-8"))
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["description", "key", "text"])
        for key, text in texts.items():
            writer.writerow([describe_key(key), key, text])
    print(OUTPUT_CSV)


if __name__ == "__main__":
    main()
