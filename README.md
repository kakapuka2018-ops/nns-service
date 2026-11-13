# NNS Service — Android + API

## Android app
The app now has a configurable **API server address** on the login screen. The address is saved on the device, so it does not need to be entered again every time.

Examples:
- Local network: `http://192.168.1.50:8080`
- HTTPS server: `https://api.example.com`

For local HTTP testing, the Android manifest allows cleartext traffic.

## Important
For a stable production setup, deploy the API to a server with a stable HTTPS domain. Then enter that domain once in the app. You will not need to rebuild the APK when the server's local IP changes.

## Сеть и IP-адрес

Приложение больше не требует менять код APK при смене локального IP. Адрес API хранится в настройках приложения и вводится один раз.

- Android Studio Emulator: `http://10.0.2.2:8080` — это специальный адрес для доступа к API на компьютере.
- Реальный телефон в той же Wi‑Fi сети: указывайте локальный IP компьютера, например `http://192.168.1.50:8080`.
- Для работы из любой сети без зависимости от IP нужен постоянный HTTPS-адрес API (домен/облачный URL). После его появления его можно прописать как значение по умолчанию, и APK не придётся пересобирать при смене IP сервера.

Также добавлена явная Android Network Security Config, разрешающая HTTP для локальной разработки.
