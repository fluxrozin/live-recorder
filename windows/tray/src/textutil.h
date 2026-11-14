#pragma once

#include <optional>
#include <string>

// Tray text rules shared with the former WinForms app: .env ports, compose
// JSON, wsl.exe output, tooltip length, and the logon Run value.

std::optional<std::string> jsonString(const std::string &json, const std::string &key);
std::optional<bool> jsonBool(const std::string &json, const std::string &key);
int countRunning(const std::string &text);

int parseWebPort(const std::string &text);
std::string webPortLines(const std::string &text);

std::string decodeOutput(const std::string &data);
std::string tipText(const std::string &text);
std::string firstLine(const std::string &text);
std::string clipText(const std::string &text, std::size_t limit);

std::string startupCommand(const std::string &exePath);
bool sameStartup(const std::string &value, const std::string &exePath);
