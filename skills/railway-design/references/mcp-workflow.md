# MCP-workflow: как чертить план и профиль инструментами этого сервера

Читай этот файл перед каждым сеансом черчения через autocad-mcp. Роутинг бэкендов общего назначения — в образце `skills/industrial-product-design-gbt/references/backend-routing.md`; здесь — конкретика этого сервера.

## 1. Инструменты сервера (12 consolidated tools)

Все инструменты принимают `operation` (+ `data` для большинства). Сигнатуры проверены по `src/autocad_mcp/server.py`:

| Tool | Ключевые operation | Заметки для ЖД |
|---|---|---|
| `system` | `preflight`, `ensure_ready`, `status`, `health`, `get_backend`, `execute_lisp`, `recover` | `preflight` без запуска AutoCAD; `ensure_ready` перед сессией |
| `drawing` | `create`/`open`/`save`/`save_as_dxf`/`plot_pdf`/`render_preview`/`info`/`context`/`activate`/`audit`/`audit_geometry`/`audit_dxf`/`setup_mechanical`/`get_variables`/`set_variables`/`undo`/`redo`/`deliver`/`workspace` | `plot_pdf` принимает `scale_mode: fit|fixed` + `scale: "paper:drawing"`; `audit_geometry` — DRC линий/полилиний |
| `entity` | `create_line/circle/polyline/rectangle/arc/tangent_arc/ellipse/mtext/text/hatch/batch`, `list/count/get`, `copy/move/rotate/scale/mirror/offset/array/fillet/chamfer/trim/extend/break/join/erase` | параметры в docstring tool'а; `create_batch` — `{entities: [...], atomic: true, continue_on_error: false, strict}` |
| `layer` | `list/create/set_current/set_properties/freeze/thaw/lock/unlock` | схема — `assets/layer-scheme.json` |
| `block` | `list/insert/insert_with_attributes/get_attributes/update_attribute/define` | условные обозначения переводов, штамп |
| `annotation` | `create_text/create_dimension_linear/aligned/angular/radius/create_leader` | высоты текстов — явно |
| `view` | `zoom_extents/fit_drawing/zoom_window/set_visual_style/get_screenshot` | скриншоты — диагностические; для верификации `render_preview` |

Мутации принимают `doc_id` + `expected_revision` (и опционально `lease_token`, `worker_generation`, `idempotency_key`) — заполняй из ответа `drawing` → `context`. `entity` по умолчанию работает в `strict`-режиме: незнакомые/лишние поля отклоняются до мутации.

## 2. Бэкенды

- **offline (ezdxf, DXF, headless)** — детерминированный; пользуйся, когда AutoCAD не нужен (заготовки, DXF-выдача, расчётные модели). Документы создаются как .dxf.
- **live AutoCAD (file-IPC, LISP/COM)** — когда нужен DWG, живой чертёж, штампы, печать.
- **нативный .NET-плагин** — когда доступен.

Выбор: минимальный бэкенд, дающий требуемые доказательства. Если живой бэкенд повторно возвращает `E_POSTCONDITION_MISMATCH` — генерируй геометрию offline (ezdxf-путь: `profile_grid.py` и скрипты скилла), а живой AutoCAD используй только на открытие/проверку/печать. Не заставляй бэкенд «изображать» чужие возможности.

## 3. Лазы надёжности этого сервера

1. **Идентичность документа (P0):** после `create`/`open`/`activate`/`save`/`save_as_dxf`/`plot_pdf`/`render_preview` вызывай `drawing` → `context`; сравнивай `doc_id`, путь, revision. Все мутации — с `doc_id` + `expected_revision` из последнего ответа. Расхождение → `E_DOCUMENT_ID_MISMATCH`, стоп этапа.
2. **Постусловия (P0):** ответы create-операций содержат requested/actual/diff (verify_created_entity). Любое необъяснённое расхождение координат/типа/слоя — стоп зависимого черчения; один ограниченный recovery (`undo` → повтор), затем смена бэкенда или `BLOCKED`.
3. **Транспорт (P0):** один FIFO-поток вызовов; никаких параллельных мутаций и скриншотов. Ошибка лимита/очереди — стоп раунда, сохранение доказательств.
4. **Ответы (P1):** не заталкивай гигантские ответы `entity.list` в контекст: пользуйся `layer`/`count`/`audit` с фильтрами; скриншоты — через `render_preview` в файл.
5. **Доказательства (P1):** веди журнал этапов: операция → requested/actual → контроль → результат. В конце — `drawing` → `deliver` (валидированный пакет DWG/DXF/PDF с SHA-256).

## 4. Типовой сеанс: от ТЗ до PDF

```text
system.preflight → system.ensure_ready (выбор бэкенда)
→ расчёты: track_geometry.py / clothoid_points.py / cant_calc.py / turnout_geometry.py (локально, без MCP)
→ drawing.create (или open) → drawing.context: зафиксировать doc_id/revision
→ слои: layer.create по layer-scheme.json → layer.list (постусловие схемы)
→ ПЛАН: entity.create_batch (оси/вставки, атомарно) → postconditions
        entity.create_arc (круговые) + create_polyline (клотоиды) → entity.get контроль НКК/ККК/СЦ
        annotation: пикетаж, ВУ/НК/КК выноски; view.zoom_extents → drawing.render_preview → осмотр
→ ПИКЕТАЖ: выноски/подписи из chainage скрипта → render_preview
→ ПРОФИЛЬ: profile_grid.py → DXF-сетка; open / вставка; полилинии земли и проектной линии;
        annotation тексты отметок/уклонов → render_preview
→ ВОЗВЫШЕНИЕ: cant_calc.py → подписи h в ведомости кривых (RW-GRADE)
→ ПЕРЕВОДЫ: turnout_geometry.py → условные обозначения (block) + подписи марок → render_preview
→ drawing.audit + drawing.audit_geometry → разбор findings, DRC-фиксы у источника
→ КОМПОНОВКА ЛИСТА: рамка/штамп (RW-FRAME), блоки атрибутов
→ drawing.save → drawing.plot_pdf {scale_mode: fixed, scale: "1:2000"} → открытие PDF-результата, осмотр
→ drawing.deliver → пакет + SHA-256 → отчёт по Output Contract (SKILL.md)
```

Точки `render_preview` — обязательные контрольные остановки: сравнивай фактическое изображение с ожиданием этапа (пустой/чужой/несоответствующий превью-артефакт = постусловие провалено). В сложных местах — `view.zoom_window` + диагностический `get_screenshot`.

## 5. Дискретизация и точность

- Круговые — `create_arc` (аналитическая дуга), клотоиды — полилиния по `clothoid_points.py` (шаг из условия стрелы прогиба, см. track-plan.md §5); число вершин указывай в отчёте.
- После `create_polyline` проверяй `entity.get`: число вершин, замкнутость (`closed: false`), совпадение первой/последней точки со стыками (допуск — 1e-6 м для аналитических стыков).
- Каждая линия оси обязана состыковываться с соседним элементом: НК=конец вставки, НКК=конец клотоиды и т.д. Нестыковки собирай в DRC-отчёт `audit_geometry`, а не «на глаз».

## 6. Откаты и границы

- `drawing.undo/redo` — откат последней операции; для атомарных групп используй `entity.create_batch` с `atomic: true` (rollback доказывается счётчиком сущностей до/после).
- Пикетажные величины и отметки не «подгоняй» в чертеже под красоту: расхождение расчёта и чертежа = дефект источника (скрипта/ввода), чинить источник и перегенерировать.
- MCP не делает Civil-corridors, коридоры 3D, поперечники и объёмы земляных работ — это фиксированное ограничение скилла (SKILL.md «Ограничения»).
