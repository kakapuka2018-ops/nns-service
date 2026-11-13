# Сборка APK через GitHub Actions

1. Загрузите **всё содержимое этой папки** в корень GitHub-репозитория.
2. В корне должны быть `.github`, `android`, `server`, `bot`.
3. Откройте GitHub → **Actions** → **Build NNS Service APK**.
4. Нажмите **Run workflow** → выберите `main` → **Run workflow**.
5. Дождитесь зелёной галочки.
6. Откройте успешный запуск → внизу **Artifacts** → `nns-service-debug-apk`.
7. Скачайте ZIP, распакуйте и получите `app-debug.apk`.

Workflow использует Java 17 и Gradle 8.10.2 и не требует Android Studio для сборки на GitHub.
