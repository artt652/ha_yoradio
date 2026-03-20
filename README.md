# 🎵 yoRadio для Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
![Version](https://img.shields.io/badge/version-0.11.0-blue.svg)
![Maintenance](https://img.shields.io/maintenance/yes/2026.svg)

Улучшенная интеграция [yoRadio](https://github.com/e2002/yoradio/tree/main/HA) для Home Assistant с расширенным функционалом и многоуровневым поиском обложек.

## ✨ Отличия от оригинальной версии

### 🎨 **Умный поиск обложек (3 уровня)**

#### 1. 🔍 **iTunes API**
- Поиск реальных обложек альбомов
- Автоматическое улучшение качества (100x100 → 400x400)
- Кэширование результатов

#### 2. 🎯 **UI Avatars** (фолбэк)
- Генерация стильных аватарок на основе инициалов
- 15 цветовых схем
- Автоматический подбор цвета на основе хэша названия
- Пример: `The Beatles - Hey Jude` → инициалы "TH" на цветном фоне

#### 3. 👤 **Gravatar** (последний шанс)
- Поиск по Gravatar на основе имени исполнителя
- Использует псевдо-email для генерации хэша

### 🛡️ **Надёжность**

- ✅ Отписка от MQTT при удалении сущности
- ✅ Защита от "гонки запросов" при смене треков
- ✅ Проверка Content-Type при загрузке плейлистов
- ✅ Расширенная обработка ошибок и таймаутов
- ✅ Фильтрация служебных сообщений (статусы, ошибки)

### 📊 **Диагностика**
- Уникальный идентификатор устройства
- Расширенные атрибуты для отладки
- Информация о размере кэша

### ⚙️ **Настраиваемые источники обложек**
```yaml
# configuration.yaml
media_player:
  - platform: yoradio
    root_topic: yoradio
    name: "Моё радио"
    cover_sources:
      - itunes      # реальные обложки
      - ui_avatars  # инициалы (фолбэк)
      - gravatar    # последний шанс
```


📦 Установка:
Через HACS (рекомендуется):

1. Добавьте этот репозиторий как пользовательский репозиторий HACS:

URL: ```https://github.com/artt652/ha_yoradio```

Категория: Integration

2. Найдите в HACS и установите "yoRadio"

Ручная установка:

1. Скопируйте папку custom_components/yoradio в директорию custom_components вашего Home Assistant

2. Перезапустите Home Assistant

🔧 Конфигурация:

Минимальная:
```yaml
media_player:
  - platform: yoradio
    root_topic: yoradio
```
Полная:
```yaml
media_player:
  - platform: yoradio
    root_topic: yoradio
    name: "Кухонное радио"
    max_volume: 255
    cover_sources:
      - itunes
      - ui_avatars
      - gravatar
```

🐛 Известные проблемы и решения:

Обложки не загружаются:

- Проверьте подключение к интернету
- Убедитесь, что iTunes API доступен
- Попробуйте изменить порядок источников в cover_sources


🤝 Приветствуются:

- Сообщения об ошибках
- Предложения по улучшению
- Pull requests

🙏 Благодарности:

e2002 - создатель оригинального компонента и прошивки yoRadio

Сообществу Home Assistant за вдохновение

iTunes API за отличный источник обложек

UI Avatars за генерацию аватарок
