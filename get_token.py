import os
import time

def get_xwb_token(headless=True):
    try:
        from seleniumbase import Driver
    except ImportError:
        raise ImportError("Установите seleniumbase: pip install seleniumbase")

    print("Запуск браузера для обхода защиты WB (JS challenge)...")
    driver = Driver(uc=True, headless=headless)

    try:
        driver.get("https://www.wildberries.ru/")
        print("Ждём прохождения проверки (5-10 сек)...")
        time.sleep(8)

        # Получаем куки
        cookies = driver.get_cookies()
        token = None
        for cookie in cookies:
            if cookie['name'] == 'xwb-token':
                token = cookie['value']
                break

        if token:
            print("Токен успешно получен!")
            with open(".token", "w") as f:
                f.write(token)
            return token
        else:
            print("ОШИБКА: xwb-token не найден в куках.")
            return None
    finally:
        driver.quit()

if __name__ == "__main__":
    get_xwb_token(headless=False)
