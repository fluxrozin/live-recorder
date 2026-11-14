#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif

#include "trayapp.h"

#include "textutil.h"

#include <QAction>
#include <QCoreApplication>
#include <QDesktopServices>
#include <QDir>
#include <QFile>
#include <QFutureWatcher>
#include <QMenu>
#include <QMessageBox>
#include <QNetworkAccessManager>
#include <QNetworkProxy>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QProcess>
#include <QSystemTrayIcon>
#include <QTimer>
#include <QUrl>
#include <QWidget>
#include <QtConcurrent>

#include <windows.h>

#include <memory>
#include <stdexcept>
#include <utility>

namespace {

const wchar_t *kRunKey = L"Software\\Microsoft\\Windows\\CurrentVersion\\Run";
const wchar_t *kRunValue = L"LiveRecorderTray";

struct ProcState {
    QByteArray out;
    QByteArray err;
    bool timedOut = false;
    bool settled = false;
    QProcess *proc = nullptr;
    QTimer *timer = nullptr;
};

std::string utf8(const QString &text) {
    const QByteArray bytes = text.toUtf8();
    return std::string(bytes.constData(), static_cast<std::size_t>(bytes.size()));
}

QString fromUtf8(const std::string &text) {
    return QString::fromUtf8(text.data(), static_cast<qsizetype>(text.size()));
}

QString windowsMessage(DWORD code) {
    wchar_t *buffer = nullptr;
    const DWORD flags =
        FORMAT_MESSAGE_ALLOCATE_BUFFER | FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS;
    const DWORD length = FormatMessageW(flags, nullptr, code, 0, reinterpret_cast<wchar_t *>(&buffer), 0, nullptr);
    QString text = length ? QString::fromWCharArray(buffer, static_cast<qsizetype>(length)).trimmed()
                          : QString::number(code);
    if (buffer)
        LocalFree(buffer);
    return text;
}

QStringList upArgs() {
    return {QStringLiteral("docker"), QStringLiteral("compose"), QStringLiteral("up"), QStringLiteral("-d")};
}

QStringList grepArgs() {
    return {QStringLiteral("grep"), QStringLiteral("-E"),
            QStringLiteral(R"(^[[:space:]]*(export[[:space:]]+)?WEB_PORT[[:space:]]*=)"),
            QStringLiteral(".env")};
}

std::optional<TrayConfig> loadConfig() {
    const QString path = QDir(QCoreApplication::applicationDirPath()).filePath(QStringLiteral("tray.json"));
    QFile file(path);
    if (!file.exists())
        return std::nullopt;
    if (!file.open(QIODevice::ReadOnly))
        throw std::runtime_error(utf8(file.errorString()));
    QString text = QString::fromUtf8(file.readAll());
    if (text.startsWith(QChar(0xFEFF)))
        text = text.mid(1);
    const std::string json = utf8(text);
    const auto distro = jsonString(json, "distro");
    const auto project = jsonString(json, "project");
    if (!distro || distro->empty() || !project || project->empty())
        return std::nullopt;
    TrayConfig config;
    config.distro = fromUtf8(*distro);
    config.project = fromUtf8(*project);
    return config;
}

std::optional<QString> readEnvFile(const TrayConfig &config) {
    QString rel = config.project;
    rel.replace(QLatin1Char('/'), QLatin1Char('\\'));
    if (rel.isEmpty() || rel.front() != QLatin1Char('\\'))
        rel.prepend(QLatin1Char('\\'));
    const QString roots[] = {QStringLiteral("\\\\wsl.localhost\\"), QStringLiteral("\\\\wsl$\\")};
    for (const QString &root : roots) {
        QFile file(root + config.distro + rel + QStringLiteral("\\.env"));
        if (!file.exists())
            continue;
        if (!file.open(QIODevice::ReadOnly))
            continue;
        const QByteArray data = file.readAll();
        const std::string decoded = decodeOutput(std::string(data.constData(), static_cast<std::size_t>(data.size())));
        const std::string lines = webPortLines(decoded);
        return fromUtf8(lines);
    }
    return std::nullopt;
}

}  // namespace

TrayApp::TrayApp(QObject *parent) : QObject(parent) {
    anchor = new QWidget;
    anchor->setWindowFlag(Qt::Tool);
    anchor->setWindowTitle(QStringLiteral("Live Recorder"));
    (void)anchor->winId();

    idleIcon = loadIcon(QStringLiteral("idle.ico"));
    recordingIcon = loadIcon(QStringLiteral("recording.ico"));
    stoppedIcon = loadIcon(QStringLiteral("stopped.ico"));
    busyIcon = loadIcon(QStringLiteral("busy.ico"));
    config = loadConfig();

    auto *menu = new QMenu(anchor);
    startAction = menu->addAction(QStringLiteral("起動"));
    stopAction = menu->addAction(QStringLiteral("停止"));
    rebuildAction = menu->addAction(QStringLiteral("再構築"));
    menu->addSeparator();
    openAction = menu->addAction(QStringLiteral("管理画面を開く"));
    menu->addSeparator();
    startupAction = menu->addAction(QStringLiteral("Windows起動時に自動実行"));
    startupAction->setCheckable(true);
    menu->addSeparator();
    exitAction = menu->addAction(QStringLiteral("停止して終了"));

    connect(startAction, &QAction::triggered, this, [this] { startStack(); });
    connect(stopAction, &QAction::triggered, this, [this] { stopStack(); });
    connect(rebuildAction, &QAction::triggered, this, [this] { rebuildStack(); });
    connect(openAction, &QAction::triggered, this, [this] { openBrowser(); });
    connect(startupAction, &QAction::triggered, this, [this] { applyStartup(); });
    connect(exitAction, &QAction::triggered, this, [this] { requestExit(); });
    connect(menu, &QMenu::aboutToShow, this, [this] { startupAction->setChecked(startupRegistered()); });

    tray = new QSystemTrayIcon(stoppedIcon, this);
    tray->setContextMenu(menu);
    tray->show();
    connect(tray, &QSystemTrayIcon::activated, this, [this](QSystemTrayIcon::ActivationReason reason) {
        if (reason == QSystemTrayIcon::DoubleClick)
            openBrowser();
    });

    health = new QNetworkAccessManager(this);
    health->setProxy(QNetworkProxy(QNetworkProxy::NoProxy));

    timer = new QTimer(this);
    timer->setInterval(5000);
    connect(timer, &QTimer::timeout, this, [this] { onTick(); });
    timer->start();

    apply(0, true, QString(), false, false);
    if (config)
        runUserCommand(upArgs(), 600000, QStringLiteral("起動中"), QStringLiteral("起動に失敗しました"), true, true);
    else
        refreshStatus();
}

QIcon TrayApp::loadIcon(const QString &fileName) const {
    const QIcon icon(QStringLiteral(":/icons/") + fileName);
    if (icon.isNull())
        throw std::runtime_error(utf8(QStringLiteral("icon resource missing: ") + fileName));
    return icon;
}

QString TrayApp::exePath() const {
    return QDir::toNativeSeparators(QCoreApplication::applicationFilePath());
}

void TrayApp::onTick() {
    if (exitRequested || busy)
        return;
    if (upPending && !healthOk && runningCount == 0) {
        runUserCommand(upArgs(), 600000, QStringLiteral("起動中"), QStringLiteral("起動に失敗しました"), true, true);
        return;
    }
    refreshStatus();
}

void TrayApp::invalidatePoll() {
    statusToken++;
    polling = false;
}

void TrayApp::refreshStatus() {
    if (polling || busy)
        return;
    if (!config) {
        apply(0, true, QString(), false, false);
        return;
    }
    polling = true;
    pollingToken = statusToken;
    const int token = pollingToken;
    runWsl({QStringLiteral("docker"), QStringLiteral("compose"), QStringLiteral("ps"), QStringLiteral("--all"),
            QStringLiteral("--format"), QStringLiteral("json")},
           20000, [this, token](const CommandResult &ps) {
               if (token != statusToken) {
                   if (pollingToken == token)
                       polling = false;
                   return;
               }
               const int running = ps.ok() ? countRunning(utf8(ps.out)) : 0;
               const bool psOk = ps.ok();
               const QString psError = fromUtf8(firstLine(utf8(ps.err)));
               withPort([this, token, running, psOk, psError](int port) {
                   if (token != statusToken) {
                       if (pollingToken == token)
                           polling = false;
                       return;
                   }
                   fetchHealth(port, [this, token, running, psOk, psError](bool ok, bool recording) {
                       if (pollingToken == token)
                           polling = false;
                       if (token != statusToken || busy)
                           return;
                       apply(running, psOk, psError, ok, recording);
                   });
               });
           });
}

void TrayApp::startStack() {
    runUserCommand(upArgs(), 600000, QStringLiteral("起動中"), QStringLiteral("起動に失敗しました"), false, true);
}

void TrayApp::stopStack() {
    upPending = false;
    runUserCommand({QStringLiteral("docker"), QStringLiteral("compose"), QStringLiteral("stop")}, 90000,
                   QStringLiteral("停止しています"), QStringLiteral("停止に失敗しました"), false, false);
}

void TrayApp::rebuildStack() {
    if (busy || !config || exitRequested)
        return;
    if (recordingNow &&
        !confirm(QStringLiteral("録画中です。再構築すると録画を区切り、コンテナを作り直します。")))
        return;
    upPending = false;
    runUserCommand({QStringLiteral("docker"), QStringLiteral("compose"), QStringLiteral("up"), QStringLiteral("-d"),
                    QStringLiteral("--build")},
                   1800000, QStringLiteral("再構築しています"), QStringLiteral("再構築に失敗しました"), false, false);
}

void TrayApp::requestExit() {
    if (exitRequested)
        return;
    if (recordingNow && !confirm(QStringLiteral("録画中です。終了すると録画を区切り、コンテナを止めます。")))
        return;
    exitRequested = true;
    upPending = false;
    tip = QStringLiteral("終了しています");
    showIcon();
    updateMenu();
    if (!busy)
        beginDown();
}

void TrayApp::beginDown() {
    if (downStarted)
        return;
    downStarted = true;
    exitRequested = true;
    upPending = false;
    invalidatePoll();
    if (!config) {
        QCoreApplication::quit();
        return;
    }
    busy = true;
    tip = QStringLiteral("終了しています");
    showIcon();
    updateMenu();
    runWsl({QStringLiteral("docker"), QStringLiteral("compose"), QStringLiteral("down")}, 90000,
           [this](const CommandResult &result) {
               if (!result.ok()) {
                   QString detail = result.err.isEmpty() ? result.out : result.err;
                   if (detail.isEmpty())
                       detail = QStringLiteral("タイムアウト");
                   QMessageBox box(QMessageBox::Critical, QStringLiteral("終了に失敗しました"),
                                   fromUtf8(clipText(utf8(detail), 800)));
                   box.setWindowFlag(Qt::WindowStaysOnTopHint, true);
                   box.exec();
               }
               QCoreApplication::quit();
           });
}

void TrayApp::runUserCommand(const QStringList &linuxArgs, int timeoutMs, const QString &progress,
                             const QString &failTitle, bool quiet, bool retryUp) {
    if (busy || !config || exitRequested)
        return;
    invalidatePoll();
    busy = true;
    if (retryUp)
        upPending = true;
    lastError.clear();
    tip = progress;
    showIcon();
    updateMenu();
    runWsl(linuxArgs, timeoutMs, [this, quiet, retryUp, failTitle](const CommandResult &result) {
        busy = false;
        if (exitRequested) {
            beginDown();
            return;
        }
        if (!result.ok()) {
            const QString detail = result.err.isEmpty() ? result.out : result.err;
            lastError = fromUtf8(firstLine(utf8(detail)));
            if (!quiet || !autoFailureNoted) {
                autoFailureNoted = true;
                balloon(failTitle, detail);
            }
        } else if (retryUp) {
            upPending = false;
            autoFailureNoted = false;
            lastError.clear();
        }
        refreshStatus();
    });
}

void TrayApp::openBrowser() {
    if (!config)
        return;
    withPort([this](int port) {
        const QUrl url(QStringLiteral("http://127.0.0.1:%1/").arg(port));
        if (!QDesktopServices::openUrl(url))
            balloon(QStringLiteral("管理画面を開けません"), url.toString());
    });
}

void TrayApp::apply(int running, bool psOk, const QString &psError, bool nextHealthOk, bool recording) {
    runningCount = running;
    healthOk = nextHealthOk;
    recordingNow = recording;
    if (!config)
        tip = QStringLiteral("tray.json がありません");
    else if (healthOk) {
        lastError.clear();
        upPending = false;
        autoFailureNoted = false;
        tip = recording ? QStringLiteral("録画中") : QStringLiteral("待機中");
    } else if (running > 0)
        tip = QStringLiteral("起動中");
    else if (!psOk)
        tip = psError.isEmpty() ? QStringLiteral("Docker に接続できません") : psError;
    else if (!lastError.isEmpty())
        tip = lastError;
    else
        tip = QStringLiteral("停止中");
    showIcon();
    updateMenu();
}

void TrayApp::showIcon() {
    QIcon icon = stoppedIcon;
    const bool starting = upPending || (runningCount > 0 && !healthOk);
    if (busy || exitRequested || starting)
        icon = busyIcon;
    else if (healthOk && recordingNow)
        icon = recordingIcon;
    else if (healthOk)
        icon = idleIcon;
    tray->setIcon(icon);
    tray->setToolTip(fromUtf8(tipText(utf8(tip))));
}

void TrayApp::updateMenu() {
    const bool have = config.has_value();
    const bool locked = busy || exitRequested;
    startAction->setEnabled(have && !locked);
    stopAction->setEnabled(have && !locked && (healthOk || runningCount > 0 || upPending));
    rebuildAction->setEnabled(have && !locked);
    openAction->setEnabled(have && !exitRequested);
    startupAction->setEnabled(!exitRequested);
    exitAction->setEnabled(!exitRequested);
}

void TrayApp::applyStartup() {
    const bool enable = startupAction->isChecked();
    HKEY key = nullptr;
    DWORD code = RegCreateKeyExW(HKEY_CURRENT_USER, kRunKey, 0, nullptr, 0, KEY_SET_VALUE, nullptr, &key, nullptr);
    if (code == ERROR_SUCCESS) {
        if (enable) {
            const std::wstring command = fromUtf8(startupCommand(utf8(exePath()))).toStdWString();
            code = RegSetValueExW(key, kRunValue, 0, REG_SZ, reinterpret_cast<const BYTE *>(command.c_str()),
                                  static_cast<DWORD>((command.size() + 1) * sizeof(wchar_t)));
        } else {
            code = RegDeleteValueW(key, kRunValue);
            if (code == ERROR_FILE_NOT_FOUND)
                code = ERROR_SUCCESS;
        }
        RegCloseKey(key);
    }
    if (code != ERROR_SUCCESS) {
        startupAction->setChecked(startupRegistered());
        balloon(QStringLiteral("自動実行を変更できません"), windowsMessage(code));
    }
}

bool TrayApp::startupRegistered() const {
    HKEY key = nullptr;
    if (RegOpenKeyExW(HKEY_CURRENT_USER, kRunKey, 0, KEY_QUERY_VALUE, &key) != ERROR_SUCCESS)
        return false;
    wchar_t buffer[32768];
    DWORD size = sizeof(buffer);
    DWORD type = 0;
    const LONG rc = RegGetValueW(key, nullptr, kRunValue, RRF_RT_REG_SZ, &type, buffer, &size);
    RegCloseKey(key);
    if (rc != ERROR_SUCCESS)
        return false;
    return sameStartup(utf8(QString::fromWCharArray(buffer)), utf8(exePath()));
}

void TrayApp::balloon(const QString &title, const QString &text) {
    QString body = text.isEmpty() ? title : text;
    body = fromUtf8(clipText(utf8(body), 200));
    tray->showMessage(title, body, QSystemTrayIcon::Critical, 4000);
}

void TrayApp::runWsl(const QStringList &linuxArgs, int timeoutMs,
                     std::function<void(const CommandResult &)> done) {
    if (!config) {
        CommandResult result;
        result.code = -1;
        result.err = QStringLiteral("wsl.exe を起動できません");
        done(result);
        return;
    }

    auto *proc = new QProcess(this);
    auto *timer = new QTimer(this);
    timer->setSingleShot(true);
    auto state = std::make_shared<ProcState>();
    state->proc = proc;
    state->timer = timer;

    QStringList args;
    args << QStringLiteral("-d") << config->distro << QStringLiteral("--cd") << config->project
         << QStringLiteral("--");
    args.append(linuxArgs);

    auto settle = [state, done = std::move(done)](CommandResult result) {
        if (state->settled)
            return;
        state->settled = true;
        state->timer->stop();
        state->proc->disconnect();
        state->proc->deleteLater();
        state->timer->deleteLater();
        done(result);
    };

    connect(proc, &QProcess::readyReadStandardOutput, this, [state, proc] {
        state->out += proc->readAllStandardOutput();
    });
    connect(proc, &QProcess::readyReadStandardError, this, [state, proc] {
        state->err += proc->readAllStandardError();
    });
    connect(timer, &QTimer::timeout, this, [state, proc] {
        if (state->settled)
            return;
        state->timedOut = true;
        proc->kill();
    });
    connect(proc, QOverload<int, QProcess::ExitStatus>::of(&QProcess::finished), this,
            [state, proc, settle](int code, QProcess::ExitStatus) {
                state->out += proc->readAllStandardOutput();
                state->err += proc->readAllStandardError();
                CommandResult result;
                result.out = fromUtf8(decodeOutput(std::string(state->out.constData(), static_cast<std::size_t>(state->out.size()))));
                if (state->timedOut) {
                    result.code = -1;
                    result.err = QStringLiteral("タイムアウト");
                } else {
                    result.code = code;
                    result.err = fromUtf8(
                        decodeOutput(std::string(state->err.constData(), static_cast<std::size_t>(state->err.size()))));
                }
                settle(result);
            });
    connect(proc, &QProcess::errorOccurred, this, [state, settle](QProcess::ProcessError error) {
        if (error != QProcess::FailedToStart || state->settled)
            return;
        CommandResult result;
        result.code = -1;
        result.err = QStringLiteral("wsl.exe を起動できません");
        settle(result);
    });

    proc->setStandardInputFile(QProcess::nullDevice());
    proc->setCreateProcessArgumentsModifier([](QProcess::CreateProcessArguments *args) {
        args->flags |= CREATE_NO_WINDOW;
        args->startupInfo->dwFlags |= STARTF_USESHOWWINDOW;
        args->startupInfo->wShowWindow = SW_HIDE;
    });
    timer->start(timeoutMs);
    proc->start(QStringLiteral("wsl.exe"), args);
}

void TrayApp::withPort(std::function<void(int)> done) {
    if (!config) {
        done(8080);
        return;
    }
    const TrayConfig local = *config;
    auto *watcher = new QFutureWatcher<std::optional<QString>>(this);
    connect(watcher, &QFutureWatcher<std::optional<QString>>::finished, this, [this, watcher, done] {
        const std::optional<QString> file = watcher->result();
        watcher->deleteLater();
        if (file.has_value()) {
            done(parseWebPort(utf8(*file)));
            return;
        }
        runWsl(grepArgs(), 15000, [done](const CommandResult &result) {
            done(parseWebPort(result.out.isEmpty() ? std::string() : utf8(result.out)));
        });
    });
    watcher->setFuture(QtConcurrent::run([local] { return readEnvFile(local); }));
}

void TrayApp::fetchHealth(int port, std::function<void(bool, bool)> done) {
    QNetworkRequest request{QUrl(QStringLiteral("http://127.0.0.1:%1/healthz").arg(port))};
    request.setTransferTimeout(3000);
    QNetworkReply *reply = health->get(request);
    connect(reply, &QNetworkReply::finished, this, [reply, done] {
        reply->deleteLater();
        const int status = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
        const QByteArray body = reply->readAll();
        if (reply->error() != QNetworkReply::NoError || status != 200) {
            done(false, false);
            return;
        }
        const std::string text(body.constData(), static_cast<std::size_t>(body.size()));
        if (jsonString(text, "status") != std::optional<std::string>("ok")) {
            done(false, false);
            return;
        }
        const auto recording = jsonBool(text, "recording");
        done(true, recording.has_value() && *recording);
    });
}

bool TrayApp::confirm(const QString &text) const {
    QMessageBox box(QMessageBox::Warning, QStringLiteral("Live Recorder"), text, QMessageBox::Ok | QMessageBox::Cancel);
    box.setDefaultButton(QMessageBox::Cancel);
    box.setWindowFlag(Qt::WindowStaysOnTopHint, true);
    return box.exec() == QMessageBox::Ok;
}
