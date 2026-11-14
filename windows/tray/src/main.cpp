#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif

#include <windows.h>

#include <QApplication>
#include <QLibraryInfo>
#include <QLocale>
#include <QMessageBox>
#include <QTranslator>

#include <exception>

#include "trayapp.h"

namespace {

enum class LockResult { Acquired, Busy, Failed };

class InstanceLock {
public:
    InstanceLock() = default;
    InstanceLock(const InstanceLock &) = delete;
    InstanceLock &operator=(const InstanceLock &) = delete;

    ~InstanceLock() {
        if (!mutex)
            return;
        ReleaseMutex(mutex);
        CloseHandle(mutex);
    }

    LockResult acquire() {
        mutex = CreateMutexW(nullptr, FALSE, L"Local\\LiveRecorderTray");
        if (!mutex)
            return LockResult::Failed;
        const DWORD wait = WaitForSingleObject(mutex, 0);
        if (wait == WAIT_OBJECT_0 || wait == WAIT_ABANDONED)
            return LockResult::Acquired;
        CloseHandle(mutex);
        mutex = nullptr;
        return wait == WAIT_TIMEOUT ? LockResult::Busy : LockResult::Failed;
    }

private:
    HANDLE mutex = nullptr;
};

}  // namespace

int main(int argc, char *argv[]) {
    InstanceLock lock;
    const LockResult lockResult = lock.acquire();
    if (lockResult == LockResult::Busy)
        return 0;
    if (lockResult == LockResult::Failed) {
        MessageBoxW(nullptr, L"多重起動の確認に失敗しました", L"Live Recorder", MB_OK | MB_ICONERROR);
        return 1;
    }

    QApplication app(argc, argv);
    QApplication::setApplicationName(QStringLiteral("Live Recorder"));
    QApplication::setQuitOnLastWindowClosed(false);

    QTranslator translator;
    const QString translations = QLibraryInfo::path(QLibraryInfo::TranslationsPath);
    if (translator.load(QLocale(), QStringLiteral("qtbase"), QStringLiteral("_"), translations))
        app.installTranslator(&translator);

    try {
        TrayApp tray;
        return app.exec();
    } catch (const std::exception &ex) {
        QMessageBox::critical(nullptr, QStringLiteral("Live Recorder"), QString::fromUtf8(ex.what()));
        return 1;
    }
}
