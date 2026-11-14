#include "textutil.h"

#include <algorithm>
#include <cstdint>
#include <vector>

namespace {

bool isSpace(unsigned char c) {
    return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\v' || c == '\f';
}

std::string trimCopy(std::string text) {
    std::size_t begin = 0;
    while (begin < text.size() && isSpace(static_cast<unsigned char>(text[begin])))
        begin++;
    std::size_t end = text.size();
    while (end > begin && isSpace(static_cast<unsigned char>(text[end - 1])))
        end--;
    return text.substr(begin, end - begin);
}

std::string trimStart(std::string text) {
    std::size_t begin = 0;
    while (begin < text.size() && isSpace(static_cast<unsigned char>(text[begin])))
        begin++;
    return text.substr(begin);
}

std::string trimQuotes(std::string text) {
    std::size_t begin = 0;
    while (begin < text.size() && text[begin] == '"')
        begin++;
    std::size_t end = text.size();
    while (end > begin && text[end - 1] == '"')
        end--;
    return text.substr(begin, end - begin);
}

std::string stripBom(std::string text) {
    if (text.size() >= 3 && static_cast<unsigned char>(text[0]) == 0xEF &&
        static_cast<unsigned char>(text[1]) == 0xBB && static_cast<unsigned char>(text[2]) == 0xBF)
        return text.substr(3);
    return text;
}

std::string stripExport(std::string line) {
    if (line.rfind("export ", 0) == 0 || line.rfind("export\t", 0) == 0)
        return trimStart(line.substr(7));
    return line;
}

std::string envValue(std::string raw) {
    if (raw.size() >= 2 && (raw[0] == '"' || raw[0] == '\'')) {
        const char quote = raw[0];
        const auto close = raw.find(quote, 1);
        if (close != std::string::npos && close >= 1)
            return trimCopy(raw.substr(1, close - 1));
    }
    auto cut = raw.find(" #");
    const auto tab = raw.find("\t#");
    if (tab != std::string::npos && (cut == std::string::npos || tab < cut))
        cut = tab;
    if (cut != std::string::npos)
        raw = raw.substr(0, cut);
    return trimCopy(raw);
}

std::vector<std::string> splitLines(std::string text) {
    text = stripBom(text);
    std::string flat;
    flat.reserve(text.size());
    for (char c : text) {
        if (c != '\r')
            flat.push_back(c);
    }
    std::vector<std::string> lines;
    std::string current;
    for (char c : flat) {
        if (c == '\n') {
            lines.push_back(current);
            current.clear();
        } else {
            current.push_back(c);
        }
    }
    lines.push_back(current);
    return lines;
}

bool isWebPortLine(const std::string &raw) {
    std::string line = trimCopy(raw);
    if (line.empty() || line[0] == '#')
        return false;
    line = stripExport(line);
    const auto eq = line.find('=');
    if (eq == std::string::npos || eq == 0)
        return false;
    return trimCopy(line.substr(0, eq)) == "WEB_PORT";
}

std::optional<int> parseInteger(const std::string &text) {
    std::size_t i = 0;
    while (i < text.size() && isSpace(static_cast<unsigned char>(text[i])))
        i++;
    if (i >= text.size())
        return std::nullopt;
    int sign = 1;
    if (text[i] == '+' || text[i] == '-') {
        if (text[i] == '-')
            sign = -1;
        i++;
    }
    if (i >= text.size() || text[i] < '0' || text[i] > '9')
        return std::nullopt;
    long long value = 0;
    while (i < text.size() && text[i] >= '0' && text[i] <= '9') {
        value = value * 10 + (text[i] - '0');
        if (value > 2147483647LL)
            return std::nullopt;
        i++;
    }
    while (i < text.size() && isSpace(static_cast<unsigned char>(text[i])))
        i++;
    if (i != text.size())
        return std::nullopt;
    if (sign < 0)
        value = -value;
    return static_cast<int>(value);
}

std::size_t skipSpace(const std::string &json, std::size_t i) {
    while (i < json.size() && isSpace(static_cast<unsigned char>(json[i])))
        i++;
    return i;
}

std::optional<std::size_t> findColon(const std::string &json, const std::string &key) {
    const std::string needle = "\"" + key + "\"";
    std::size_t from = 0;
    while (from < json.size()) {
        const auto at = json.find(needle, from);
        if (at == std::string::npos)
            return std::nullopt;
        const std::size_t i = skipSpace(json, at + needle.size());
        if (i < json.size() && json[i] == ':')
            return i;
        from = at + needle.size();
    }
    return std::nullopt;
}

std::vector<std::string> jsonObjects(const std::string &text) {
    std::vector<std::string> list;
    if (text.empty())
        return list;
    int depth = 0;
    std::size_t start = std::string::npos;
    bool inString = false;
    bool escape = false;
    for (std::size_t i = 0; i < text.size(); i++) {
        const char c = text[i];
        if (inString) {
            if (escape)
                escape = false;
            else if (c == '\\')
                escape = true;
            else if (c == '"')
                inString = false;
            continue;
        }
        if (c == '"')
            inString = true;
        else if (c == '{') {
            if (depth == 0)
                start = i;
            depth++;
        } else if (c == '}') {
            depth--;
            if (depth == 0 && start != std::string::npos) {
                list.push_back(text.substr(start, i - start + 1));
                start = std::string::npos;
            }
        }
    }
    return list;
}

bool decodeCp(const std::string &text, std::size_t i, std::uint32_t &cp, std::size_t &len) {
    if (i >= text.size())
        return false;
    const auto c0 = static_cast<unsigned char>(text[i]);
    if (c0 < 0x80) {
        cp = c0;
        len = 1;
        return true;
    }
    if ((c0 & 0xE0) == 0xC0 && i + 1 < text.size()) {
        cp = (static_cast<std::uint32_t>(c0 & 0x1F) << 6) |
             static_cast<unsigned char>(text[i + 1] & 0x3F);
        len = 2;
        return true;
    }
    if ((c0 & 0xF0) == 0xE0 && i + 2 < text.size()) {
        cp = (static_cast<std::uint32_t>(c0 & 0x0F) << 12) |
             (static_cast<std::uint32_t>(static_cast<unsigned char>(text[i + 1]) & 0x3F) << 6) |
             static_cast<unsigned char>(text[i + 2] & 0x3F);
        len = 3;
        return true;
    }
    if ((c0 & 0xF8) == 0xF0 && i + 3 < text.size()) {
        cp = (static_cast<std::uint32_t>(c0 & 0x07) << 18) |
             (static_cast<std::uint32_t>(static_cast<unsigned char>(text[i + 1]) & 0x3F) << 12) |
             (static_cast<std::uint32_t>(static_cast<unsigned char>(text[i + 2]) & 0x3F) << 6) |
             static_cast<unsigned char>(text[i + 3] & 0x3F);
        len = 4;
        return true;
    }
    cp = c0;
    len = 1;
    return true;
}

std::size_t utf16Length(const std::string &text) {
    std::size_t count = 0;
    for (std::size_t i = 0; i < text.size();) {
        std::uint32_t cp = 0;
        std::size_t len = 1;
        if (!decodeCp(text, i, cp, len))
            break;
        count += cp > 0xFFFF ? 2 : 1;
        i += len;
    }
    return count;
}

std::string prefixUtf16(const std::string &text, std::size_t limit) {
    std::size_t count = 0;
    std::size_t i = 0;
    while (i < text.size()) {
        std::uint32_t cp = 0;
        std::size_t len = 1;
        if (!decodeCp(text, i, cp, len))
            break;
        const std::size_t units = cp > 0xFFFF ? 2 : 1;
        if (count + units > limit)
            break;
        count += units;
        i += len;
    }
    return text.substr(0, i);
}

void appendUtf8(std::string &out, std::uint32_t cp) {
    if (cp <= 0x7F) {
        out.push_back(static_cast<char>(cp));
    } else if (cp <= 0x7FF) {
        out.push_back(static_cast<char>(0xC0 | (cp >> 6)));
        out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    } else if (cp <= 0xFFFF) {
        out.push_back(static_cast<char>(0xE0 | (cp >> 12)));
        out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
        out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    } else {
        out.push_back(static_cast<char>(0xF0 | (cp >> 18)));
        out.push_back(static_cast<char>(0x80 | ((cp >> 12) & 0x3F)));
        out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
        out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    }
}

std::string utf16LeToUtf8(const char *data, std::size_t size) {
    std::string out;
    std::size_t i = 0;
    while (i + 1 < size) {
        const auto lo = static_cast<unsigned char>(data[i]);
        const auto hi = static_cast<unsigned char>(data[i + 1]);
        i += 2;
        std::uint32_t cu = lo | (static_cast<std::uint32_t>(hi) << 8);
        if (cu >= 0xD800 && cu <= 0xDBFF && i + 1 < size) {
            const auto lo2 = static_cast<unsigned char>(data[i]);
            const auto hi2 = static_cast<unsigned char>(data[i + 1]);
            const std::uint32_t cu2 = lo2 | (static_cast<std::uint32_t>(hi2) << 8);
            if (cu2 >= 0xDC00 && cu2 <= 0xDFFF) {
                i += 2;
                cu = 0x10000 + (((cu - 0xD800) << 10) | (cu2 - 0xDC00));
            }
        }
        appendUtf8(out, cu);
    }
    return out;
}

bool looksUtf16Le(const std::string &data) {
    const int samples = std::min(static_cast<int>(data.size() / 2), 32);
    if (samples < 4)
        return false;
    int zeros = 0;
    for (int i = 0; i < samples; i++) {
        if (static_cast<unsigned char>(data[static_cast<std::size_t>(i) * 2 + 1]) == 0)
            zeros++;
    }
    return zeros > samples / 2;
}

std::string lowerAscii(std::string value) {
    for (char &c : value) {
        if (c >= 'A' && c <= 'Z')
            c = static_cast<char>(c - 'A' + 'a');
    }
    return value;
}

}  // namespace

std::optional<std::string> jsonString(const std::string &json, const std::string &key) {
    const auto colon = findColon(json, key);
    if (!colon)
        return std::nullopt;
    const std::size_t i = skipSpace(json, *colon + 1);
    if (i >= json.size() || json[i] != '"')
        return std::nullopt;
    std::string value;
    for (std::size_t p = i + 1; p < json.size(); p++) {
        const char c = json[p];
        if (c == '\\' && p + 1 < json.size()) {
            p++;
            const char n = json[p];
            if (n == 'n')
                value.push_back('\n');
            else if (n == 'r')
                value.push_back('\r');
            else if (n == 't')
                value.push_back('\t');
            else
                value.push_back(n);
            continue;
        }
        if (c == '"')
            return value;
        value.push_back(c);
    }
    return std::nullopt;
}

std::optional<bool> jsonBool(const std::string &json, const std::string &key) {
    const auto colon = findColon(json, key);
    if (!colon)
        return std::nullopt;
    const std::size_t i = skipSpace(json, *colon + 1);
    if (i + 4 <= json.size() && json.compare(i, 4, "true") == 0)
        return true;
    if (i + 5 <= json.size() && json.compare(i, 5, "false") == 0)
        return false;
    return std::nullopt;
}

int countRunning(const std::string &text) {
    int running = 0;
    for (const std::string &obj : jsonObjects(text)) {
        const auto state = jsonString(obj, "State");
        if (state && *state == "running")
            running++;
    }
    return running;
}

int parseWebPort(const std::string &text) {
    int port = 8080;
    if (text.empty())
        return port;
    for (const std::string &raw : splitLines(text)) {
        std::string line = trimCopy(raw);
        if (line.empty() || line[0] == '#')
            continue;
        line = stripExport(line);
        const auto eq = line.find('=');
        if (eq == std::string::npos || eq == 0)
            continue;
        if (trimCopy(line.substr(0, eq)) != "WEB_PORT")
            continue;
        const auto parsed = parseInteger(envValue(trimCopy(line.substr(eq + 1))));
        if (parsed && *parsed >= 1 && *parsed <= 65535)
            port = *parsed;
        else
            port = 8080;
    }
    return port;
}

std::string webPortLines(const std::string &text) {
    std::string out;
    for (const std::string &raw : splitLines(text)) {
        if (!isWebPortLine(raw))
            continue;
        out += raw;
        out.push_back('\n');
    }
    return out;
}

std::string decodeOutput(const std::string &data) {
    if (data.size() >= 2 && static_cast<unsigned char>(data[0]) == 0xFF &&
        static_cast<unsigned char>(data[1]) == 0xFE)
        return utf16LeToUtf8(data.data() + 2, data.size() - 2);
    if (looksUtf16Le(data))
        return utf16LeToUtf8(data.data(), data.size());
    return data;
}

std::string tipText(const std::string &text) {
    std::string flat = text.empty() ? std::string("Live Recorder") : text;
    for (char &c : flat) {
        if (c == '\r' || c == '\n')
            c = ' ';
    }
    if (utf16Length(flat) <= 63)
        return flat;
    return prefixUtf16(flat, 60) + "...";
}

std::string firstLine(const std::string &text) {
    std::string flat = trimCopy(text);
    const auto n = flat.find('\n');
    if (n != std::string::npos)
        flat = trimCopy(flat.substr(0, n));
    return flat;
}

std::string clipText(const std::string &text, std::size_t limit) {
    std::string flat;
    flat.reserve(text.size());
    for (char c : text) {
        if (c != '\r')
            flat.push_back(c);
    }
    flat = trimCopy(flat);
    if (utf16Length(flat) > limit)
        flat = prefixUtf16(flat, limit);
    return flat;
}

std::string startupCommand(const std::string &exePath) {
    std::string path;
    path.reserve(exePath.size());
    for (char c : exePath) {
        if (c != '"')
            path.push_back(c);
    }
    return "\"" + path + "\"";
}

bool sameStartup(const std::string &value, const std::string &exePath) {
    if (value.empty())
        return false;
    if (lowerAscii(value) == lowerAscii(startupCommand(exePath)))
        return true;
    return lowerAscii(trimQuotes(trimCopy(value))) == lowerAscii(exePath);
}
