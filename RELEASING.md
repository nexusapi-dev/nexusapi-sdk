# Releasing SDKs

Оба SDK публикуются GitHub Actions при пуше тега. Токенов PyPI и npm в
репозитории нет: используется **Trusted Publishing (OIDC)** — PyPI и npm
доверяют конкретному workflow этого репозитория, а сам workflow получает
одноразовый токен на время запуска.

| Пакет | Каталог | Тег | Workflow | Environment |
|---|---|---|---|---|
| PyPI `nexusapi-sdk` | `python/` | `python-sdk-v<версия>` | `.github/workflows/publish-python.yml` | `pypi` |
| npm `nexusapi-sdk` | `node/` | `node-sdk-v<версия>` | `.github/workflows/publish-node.yml` | `npm` |

## Выпуск версии

```bash
# 1. Версия: python/pyproject.toml + python/nexusapi/__init__.py (__version__)
#    и node/package.json + node/package-lock.json + VERSION в node/src/client.ts
#    (CI-задача «Versions in sync» проверяет, что везде одинаково)
# 2. Запись «## <версия> — <дата>» в CHANGELOG.md
# 3. Коммит в main, CI зелёный
git commit -am "Release 0.3.1"
git push origin main

# 4. Теги — каждый запускает свой workflow
git tag python-sdk-v0.3.1
git tag node-sdk-v0.3.1
git push origin python-sdk-v0.3.1 node-sdk-v0.3.1
```

Каждый workflow публикации:
1. запускает полный CI (`ci.yml`: все версии Python и Node, сверка версий);
2. проверяет, что тег совпадает с версией пакета и что коммит есть в `main`;
3. собирает пакет в задаче без права публикации;
4. ждёт подтверждения в окружении `pypi` / `npm` (Actions → запуск →
   **Review deployments → Approve**) и публикует собранный пакет.

У npm-пакета будет provenance («Built and signed on GitHub Actions»), у PyPI —
attestations. Теги `python-sdk-v*` / `node-sdk-v*` может создать только админ
репозитория.

**Проверка без публикации:** Actions → нужный workflow → «Run workflow».
Ручной запуск только собирает и проверяет пакет, ничего не загружает.

## Настройка Trusted Publishing (один раз)

### PyPI

1. pypi.org → проект `nexusapi-sdk` → **Manage** → **Publishing** →
   **Add a new publisher** → GitHub.
2. Owner и Repository name — этот репозиторий; Workflow name —
   `publish-python.yml`; Environment name — `pypi`.
3. После первой успешной публикации удалите старый API-токен проекта
   (pypi.org → Account settings → API tokens).

### npm

1. npmjs.com → пакет `nexusapi-sdk` → **Settings** → **Trusted Publisher** →
   **GitHub Actions**.
2. Organization or user и Repository — этот репозиторий; Workflow filename —
   `publish-node.yml`; Environment name — `npm`.
3. Там же в **Publishing access** можно включить «Require two-factor
   authentication and disallow tokens» — тогда опубликовать пакет сможет только
   этот workflow.
4. После первой успешной публикации отзовите старый granular-токен
   (npmjs.com → Access Tokens).

### GitHub

Settings → Environments: окружения `pypi` и `npm` создаются при первом
запуске сами. Чтобы публикация шла только после подтверждения, добавьте в них
**Required reviewers** и ограничьте **Deployment branches and tags** тегами
`python-sdk-v*` / `node-sdk-v*`.

## Версионирование

[SemVer](https://semver.org/):

- **patch** (0.3.0 → 0.3.1) — исправления, документация;
- **minor** (0.3.0 → 0.4.0) — новые методы и параметры, обратно совместимо;
- **major** (0.x → 1.0.0) — несовместимые изменения публичного API.

## Чек-лист перед релизом

- [ ] Версии бампнуты во всех местах (см. шаг 1), запись в `CHANGELOG.md`
- [ ] Новые методы описаны в README пакета
- [ ] Локально: `python -m build && twine check dist/*`, `npm test && npm pack --dry-run`
- [ ] CI на main зелёный
- [ ] Запушены теги `python-sdk-v*` / `node-sdk-v*`
- [ ] Workflow публикации зелёный, новая версия видна на PyPI / npm
