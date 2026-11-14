#include "src/textutil.h"

#include <cstdlib>
#include <iostream>
#include <string>

namespace {

int failures = 0;

void expect(bool cond, const std::string &message) {
    if (cond)
        return;
    std::cerr << message << '\n';
    failures++;
}

void expectEq(const std::string &actual, const std::string &want, const std::string &message) {
    if (actual == want)
        return;
    std::cerr << message << " actual=[" << actual << "] want=[" << want << "]\n";
    failures++;
}

void expectEq(int actual, int want, const std::string &message) {
    if (actual == want)
        return;
    std::cerr << message << " actual=" << actual << " want=" << want << '\n';
    failures++;
}

std::string utf16le(const char16_t *text) {
    std::string out;
    for (const char16_t *p = text; *p; ++p) {
        out.push_back(static_cast<char>(*p & 0xFF));
        out.push_back(static_cast<char>((*p >> 8) & 0xFF));
    }
    return out;
}

void testJson() {
    const auto value = jsonString("{\"a\":\"x\\ny\\t\\\"z\"}", "a");
    expect(value.has_value(), "json string present");
    expectEq(*value, "x\ny\t\"z", "json string escapes");
    expect(!jsonString("{\"a\":1}", "a").has_value(), "json string rejects numbers");
    expect(!jsonString("{}", "missing").has_value(), "json string missing");

    const auto yes = jsonBool("{\"recording\": true}", "recording");
    const auto no = jsonBool("{\"recording\":false}", "recording");
    expect(yes.has_value() && *yes, "json true");
    expect(no.has_value() && !*no, "json false");
    expect(!jsonBool("{}", "recording").has_value(), "json bool missing");

    const std::string ndjson =
        "{\"State\":\"exited\"}\n{\"Name\":\"app\",\"State\":\"running\"}\n{\"State\":\"running\"}";
    expectEq(countRunning(ndjson), 2, "ndjson running count");
    const std::string array = "[{\"State\":\"running\"},{\"State\":\"run}ning\"}]";
    expectEq(countRunning(array), 1, "array hides brace inside a string");
    expectEq(countRunning(""), 0, "empty compose output");
}

void testEnv() {
    expectEq(parseWebPort(""), 8080, "empty env");
    expectEq(parseWebPort("# WEB_PORT=1\nWEB_PORT=9090\n"), 9090, "skip comments");
    expectEq(parseWebPort("export WEB_PORT=\"9091\"\n"), 9091, "export and quotes");
    expectEq(parseWebPort("export\tWEB_PORT=7070\n"), 7070, "export tab");
    expectEq(parseWebPort("WEB_PORT = 9092 # comment\n"), 9092, "inline comment");
    expectEq(parseWebPort("WEB_PORT=82\t# comment\n"), 82, "tab comment");
    expectEq(parseWebPort("WEB_PORT=\" 81 \"\n"), 81, "quoted spaces");
    expectEq(parseWebPort("WEB_PORT=9090\nWEB_PORT=nope\n"), 8080, "invalid resets");
    expectEq(parseWebPort("WEB_PORT=0\n"), 8080, "zero is invalid");
    expectEq(parseWebPort("WEB_PORT=65535\n"), 65535, "max port");
    expectEq(parseWebPort("WEB_PORT=65536\n"), 8080, "port above range");
    expectEq(parseWebPort("WEB_PORT=+9090\n"), 9090, "leading plus");
    expectEq(parseWebPort("WEB_PORT=-1\n"), 8080, "negative");
    expectEq(parseWebPort("WEB_PORT=080\n"), 80, "leading zeros");
    expectEq(parseWebPort("WEB_PORT=9999999999\n"), 8080, "overflow");
    expectEq(parseWebPort(std::string("\xEF\xBB\xBFWEB_PORT=9093\n")), 9093, "utf-8 bom");
    expectEq(parseWebPort("OTHER=1\n"), 8080, "unrelated key");

    const std::string filtered = webPortLines("SESSION=abc\n# WEB_PORT=1\nWEB_PORT=9\nexport WEB_PORT=10\n");
    expectEq(parseWebPort(filtered), 10, "web port lines keep the last assignment");
    expect(filtered.find("SESSION") == std::string::npos, "web port lines drop secrets");
}

void testDecode() {
    expectEq(decodeOutput("停止中"), "停止中", "utf-8 passes through");
    const std::string bom = std::string("\xFF\xFE") + utf16le(u"ok");
    expectEq(decodeOutput(bom), "ok", "utf-16 bom");
    expectEq(decodeOutput(utf16le(u"running")), "running", "utf-16 heuristic");
    const std::string japanese = std::string("\xFF\xFE") + utf16le(u"\u505C\u6B62");
    expectEq(decodeOutput(japanese), "停止", "utf-16 japanese bom");
    const std::string json = "{\"State\":\"running\"}";
    expectEq(decodeOutput(json), json, "ascii json is not utf-16");
}

void testClip() {
    expectEq(tipText(""), "Live Recorder", "empty tip");
    expectEq(tipText("a\r\nb"), "a  b", "tip newlines become spaces");
    expectEq(tipText(std::string(63, 'a')), std::string(63, 'a'), "tip keeps 63");
    expectEq(tipText(std::string(64, 'a')), std::string(60, 'a') + "...", "tip clips 64");
    expectEq(tipText("あああ"), "あああ", "tip keeps japanese");

    expectEq(firstLine("  \nsecond"), "second", "leading blank line is whitespace");
    expectEq(firstLine(" alpha \r\nbeta"), "alpha", "first line trims");
    expectEq(clipText(std::string("\r\n") + std::string(801, 'b'), 800), std::string(800, 'b'), "clip 800");
    expectEq(clipText("あああ", 2), "ああ", "clip does not split a character");
}

void testStartup() {
    const std::string exe = "C:\\Apps\\LiveRecorderTray.exe";
    expectEq(startupCommand(exe), "\"" + exe + "\"", "startup command quotes the exe");
    expect(sameStartup("\"" + exe + "\"", exe), "exact startup command");
    expect(sameStartup("\"c:\\apps\\liverecordertray.exe\"", exe), "startup command ignores case");
    expect(sameStartup("  \"" + exe + "\"  ", exe), "startup value trims quotes");
    expect(!sameStartup("", exe), "empty startup value");
    expect(!sameStartup("\"C:\\Other\\LiveRecorderTray.exe\"", exe), "different exe");
}

}  // namespace

int main() {
    testJson();
    testEnv();
    testDecode();
    testClip();
    testStartup();
    if (failures != 0) {
        std::cerr << failures << " failure(s)\n";
        return EXIT_FAILURE;
    }
    return EXIT_SUCCESS;
}
