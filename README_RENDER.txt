БРИДЖ РУБИКА — ONLINE TEST НА RENDER

1. Создай GitHub-репозиторий bridge-rubik-online.
2. Загрузи в корень все файлы из этой папки, включая папку public.
3. На render.com: New -> Web Service -> подключи репозиторий.
4. Выбери план Free.
5. Если настройки не подхватились автоматически:
   Runtime: Python
   Build Command: python -m compileall server.py
   Start Command: python server.py
   Health Check Path: /healthz
6. Deploy.
7. После запуска получишь https://....onrender.com
8. Открой адрес, создай комнату и отправь ссылку друзьям.

Важно: комнаты сейчас живут только в памяти процесса. После перезапуска/засыпания тестовые комнаты исчезают.
