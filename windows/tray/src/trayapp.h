#pragma once

#include <QIcon>
#include <QObject>
#include <QString>
#include <QStringList>

#include <functional>
#include <optional>

class QAction;
class QNetworkAccessManager;
class QSystemTrayIcon;
class QTimer;
class QWidget;

struct TrayConfig {
    QString distro;
    QString project;
};

struct CommandResult {
    int code = -1;
    QString out;
    QString err;
    bool ok() const { return code == 0; }
};

class TrayApp final : public QObject {
    Q_OBJECT

public:
    explicit TrayApp(QObject *parent = nullptr);

private:
    void onTick();
    void refreshStatus();
    void invalidatePoll();
    void runUserCommand(const QStringList &linuxArgs, int timeoutMs, const QString &progress,
                        const QString &failTitle, bool quiet, bool retryUp);
    void startStack();
    void stopStack();
    void rebuildStack();
    void requestExit();
    void beginDown();
    void openBrowser();
    void apply(int running, bool psOk, const QString &psError, bool healthOk, bool recording);
    void showIcon();
    void updateMenu();
    void applyStartup();
    bool startupRegistered() const;
    void balloon(const QString &title, const QString &text);
    void runWsl(const QStringList &linuxArgs, int timeoutMs,
                std::function<void(const CommandResult &)> done);
    void withPort(std::function<void(int)> done);
    void fetchHealth(int port, std::function<void(bool, bool)> done);
    QString exePath() const;
    bool confirm(const QString &text) const;
    QIcon loadIcon(const QString &fileName) const;

    QWidget *anchor = nullptr;
    QSystemTrayIcon *tray = nullptr;
    QTimer *timer = nullptr;
    QNetworkAccessManager *health = nullptr;
    QAction *startAction = nullptr;
    QAction *stopAction = nullptr;
    QAction *rebuildAction = nullptr;
    QAction *openAction = nullptr;
    QAction *startupAction = nullptr;
    QAction *exitAction = nullptr;
    QIcon idleIcon;
    QIcon recordingIcon;
    QIcon stoppedIcon;
    QIcon busyIcon;
    std::optional<TrayConfig> config;
    bool busy = false;
    bool polling = false;
    bool upPending = false;
    bool exitRequested = false;
    bool downStarted = false;
    bool autoFailureNoted = false;
    int runningCount = 0;
    bool healthOk = false;
    bool recordingNow = false;
    QString tip = QStringLiteral("停止中");
    QString lastError;
    int statusToken = 0;
    int pollingToken = -1;
};
