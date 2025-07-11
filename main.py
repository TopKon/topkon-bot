# chatbot_prayer.py

# Словарь «триггер→ответ»
responses = {
    "привет": "Оче нас, ежи еси на небеси, гта мне запусти"
}

def get_response(user_message: str) -> str:
    """
    Возвращает ответ бота на сообщение пользователя.
    Если сообщение не распознано, возвращает заглушку.
    """
    key = user_message.strip().lower()
    return responses.get(key, "Извините, я вас не понял.")

if __name__ == "__main__":
    user_msg = input("Пользователь: ")
    bot_reply = get_response(user_msg)
    print("Бот:", bot_reply)


























