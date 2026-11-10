using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Net;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Windows.Forms;

sealed class TrayConfig
{
    public string Distro;
    public string Project;
}

sealed class CommandResult
{
    public int Code;
    public string Stdout;
    public string Stderr;

    public bool Ok { get { return Code == 0; } }
}

sealed class HealthResult
{
    public bool Ok;
    public bool Recording;
}

static class JsonRead
{
    public static string String(string json, string key)
    {
        int colon = Colon(json, key);
        if (colon < 0) return null;
        int i = Skip(json, colon + 1);
        if (i >= json.Length || json[i] != '"') return null;
        var sb = new StringBuilder();
        for (int p = i + 1; p < json.Length; p++)
        {
            char c = json[p];
            if (c == '\\' && p + 1 < json.Length)
            {
                p++;
                char n = json[p];
                if (n == 'n') sb.Append('\n');
                else if (n == 'r') sb.Append('\r');
                else if (n == 't') sb.Append('\t');
                else sb.Append(n);
                continue;
            }
            if (c == '"') return sb.ToString();
            sb.Append(c);
        }
        return null;
    }

    public static bool? Bool(string json, string key)
    {
        int colon = Colon(json, key);
        if (colon < 0) return null;
        int i = Skip(json, colon + 1);
        if (i + 4 <= json.Length && json.Substring(i, 4) == "true") return true;
        if (i + 5 <= json.Length && json.Substring(i, 5) == "false") return false;
        return null;
    }

    static int Colon(string json, string key)
    {
        string needle = "\"" + key + "\"";
        int from = 0;
        while (from < json.Length)
        {
            int at = json.IndexOf(needle, from, StringComparison.Ordinal);
            if (at < 0) return -1;
            int i = Skip(json, at + needle.Length);
            if (i < json.Length && json[i] == ':') return i;
            from = at + needle.Length;
        }
        return -1;
    }

    static int Skip(string json, int i)
    {
        while (i < json.Length && char.IsWhiteSpace(json[i])) i++;
        return i;
    }
}

sealed class TrayContext : ApplicationContext
{
    readonly List<MemoryStream> _iconStreams = new List<MemoryStream>();
    readonly Form _gate;
    readonly NotifyIcon _notify;
    readonly System.Windows.Forms.Timer _timer;
    readonly ToolStripMenuItem _start;
    readonly ToolStripMenuItem _stop;
    readonly ToolStripMenuItem _rebuild;
    readonly ToolStripMenuItem _open;
    readonly ToolStripMenuItem _exit;
    readonly Icon _iconIdle;
    readonly Icon _iconRecording;
    readonly Icon _iconStopped;
    readonly TrayConfig _config;
    bool _busy;
    bool _polling;
    bool _upPending;
    bool _exitRequested;
    bool _downStarted;
    bool _autoFailureNoted;
    int _running;
    bool _healthOk;
    bool _recording;
    string _tip = "停止中";
    string _lastError;

    public TrayContext()
    {
        _gate = new Form();
        _gate.ShowInTaskbar = false;
        if (_gate.Handle == IntPtr.Zero) throw new InvalidOperationException("window");

        _iconIdle = LoadIcon("idle.ico");
        _iconRecording = LoadIcon("recording.ico");
        _iconStopped = LoadIcon("stopped.ico");
        _config = LoadConfig();

        _start = new ToolStripMenuItem("起動");
        _start.Click += delegate { StartStack(); };
        _stop = new ToolStripMenuItem("停止");
        _stop.Click += delegate { StopStack(); };
        _rebuild = new ToolStripMenuItem("再構築");
        _rebuild.Click += delegate { RebuildStack(); };
        _open = new ToolStripMenuItem("管理画面を開く");
        _open.Click += delegate { OpenBrowser(); };
        _exit = new ToolStripMenuItem("停止して終了");
        _exit.Click += delegate { RequestExit(); };

        var menu = new ContextMenuStrip();
        menu.Items.Add(_start);
        menu.Items.Add(_stop);
        menu.Items.Add(_rebuild);
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add(_open);
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add(_exit);

        _notify = new NotifyIcon();
        _notify.Icon = _iconStopped;
        _notify.ContextMenuStrip = menu;
        _notify.Visible = true;
        _notify.DoubleClick += delegate { OpenBrowser(); };

        _timer = new System.Windows.Forms.Timer();
        _timer.Interval = 5000;
        _timer.Tick += OnTick;
        _timer.Start();
        Apply(0, true, "", false, false);
        if (_config != null)
            RunUserCommand("up -d", 600000, "起動中", "起動に失敗しました", true, true);
        else
            RefreshStatus();
    }

    protected override void ExitThreadCore()
    {
        _timer.Stop();
        _notify.Visible = false;
        _notify.Dispose();
        _gate.Dispose();
        base.ExitThreadCore();
    }

    Icon LoadIcon(string name)
    {
        using (var input = Assembly.GetExecutingAssembly().GetManifestResourceStream(name))
        {
            if (input == null) throw new InvalidOperationException("icon resource missing: " + name);
            var ms = new MemoryStream();
            input.CopyTo(ms);
            ms.Position = 0;
            _iconStreams.Add(ms);
            return new Icon(ms);
        }
    }

    static TrayConfig LoadConfig()
    {
        string dir = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
        string path = Path.Combine(dir, "tray.json");
        if (!File.Exists(path)) return null;
        string text = File.ReadAllText(path, Encoding.UTF8);
        string distro = JsonRead.String(text, "distro");
        string project = JsonRead.String(text, "project");
        if (string.IsNullOrEmpty(distro) || string.IsNullOrEmpty(project)) return null;
        return new TrayConfig { Distro = distro, Project = project };
    }

    void OnTick(object sender, EventArgs e)
    {
        if (_exitRequested || _busy) return;
        if (_upPending && !_healthOk && _running == 0)
        {
            RunUserCommand("up -d", 600000, "起動中", "起動に失敗しました", true, true);
            return;
        }
        RefreshStatus();
    }

    void Post(Action action)
    {
        if (_gate.IsDisposed) return;
        try
        {
            _gate.BeginInvoke(action);
        }
        catch (InvalidOperationException)
        {
        }
    }

    void RefreshStatus()
    {
        if (_polling || _busy) return;
        if (_config == null)
        {
            Apply(0, true, "", false, false);
            return;
        }
        _polling = true;
        var config = _config;
        ThreadPool.QueueUserWorkItem(delegate
        {
            var ps = RunWsl(config, "ps --all --format json", 20000);
            int running = ps.Ok ? CountRunning(ps.Stdout) : 0;
            var health = FetchHealth(ReadWebPort(config));
            Post(delegate
            {
                _polling = false;
                if (_busy) return;
                Apply(running, ps.Ok, FirstLine(ps.Stderr), health.Ok, health.Recording);
            });
        });
    }

    void StartStack()
    {
        RunUserCommand("up -d", 600000, "起動中", "起動に失敗しました", false, true);
    }

    void StopStack()
    {
        _upPending = false;
        RunUserCommand("stop", 90000, "停止しています", "停止に失敗しました", false, false);
    }

    void RebuildStack()
    {
        if (_busy || _config == null || _exitRequested) return;
        if (_recording)
        {
            var answer = MessageBox.Show(
                _gate,
                "録画中です。再構築すると録画を区切り、コンテナを作り直します。",
                "Live Recorder",
                MessageBoxButtons.OKCancel,
                MessageBoxIcon.Warning,
                MessageBoxDefaultButton.Button2,
                MessageBoxOptions.DefaultDesktopOnly);
            if (answer != DialogResult.OK) return;
        }
        _upPending = false;
        RunUserCommand("up -d --build", 1800000, "再構築しています", "再構築に失敗しました", false, false);
    }

    void RequestExit()
    {
        if (_exitRequested) return;
        if (_recording)
        {
            var answer = MessageBox.Show(
                _gate,
                "録画中です。終了すると録画を区切り、コンテナを止めます。",
                "Live Recorder",
                MessageBoxButtons.OKCancel,
                MessageBoxIcon.Warning,
                MessageBoxDefaultButton.Button2,
                MessageBoxOptions.DefaultDesktopOnly);
            if (answer != DialogResult.OK) return;
        }
        _exitRequested = true;
        _upPending = false;
        _tip = "終了しています";
        ShowIcon();
        UpdateMenu();
        if (!_busy) BeginDown();
    }

    void BeginDown()
    {
        if (_downStarted) return;
        _downStarted = true;
        _exitRequested = true;
        _upPending = false;
        if (_config == null)
        {
            ExitThread();
            return;
        }
        _busy = true;
        _tip = "終了しています";
        ShowIcon();
        UpdateMenu();
        var config = _config;
        ThreadPool.QueueUserWorkItem(delegate
        {
            var result = RunWsl(config, "down", 90000);
            Post(delegate
            {
                if (!result.Ok)
                {
                    string detail = string.IsNullOrEmpty(result.Stderr) ? result.Stdout : result.Stderr;
                    if (string.IsNullOrEmpty(detail)) detail = "タイムアウト";
                    MessageBox.Show(
                        _gate,
                        BoxText(detail),
                        "終了に失敗しました",
                        MessageBoxButtons.OK,
                        MessageBoxIcon.Error,
                        MessageBoxDefaultButton.Button1,
                        MessageBoxOptions.DefaultDesktopOnly);
                }
                ExitThread();
            });
        });
    }

    static string BoxText(string text)
    {
        if (string.IsNullOrEmpty(text)) return "";
        text = text.Replace("\r", "").Trim();
        if (text.Length > 800) text = text.Substring(0, 800);
        return text;
    }

    void RunUserCommand(string composeArgs, int timeoutMs, string progress, string failTitle, bool quiet, bool retryUp)
    {
        if (_busy || _config == null || _exitRequested) return;
        _busy = true;
        if (retryUp) _upPending = true;
        _lastError = null;
        _tip = progress;
        ShowIcon();
        UpdateMenu();
        var config = _config;
        ThreadPool.QueueUserWorkItem(delegate
        {
            var result = RunWsl(config, composeArgs, timeoutMs);
            Post(delegate
            {
                _busy = false;
                if (_exitRequested)
                {
                    BeginDown();
                    return;
                }
                if (!result.Ok)
                {
                    string detail = string.IsNullOrEmpty(result.Stderr) ? result.Stdout : result.Stderr;
                    _lastError = FirstLine(detail);
                    if (!quiet || !_autoFailureNoted)
                    {
                        _autoFailureNoted = true;
                        Balloon(failTitle, detail);
                    }
                }
                else if (retryUp)
                {
                    _upPending = false;
                    _autoFailureNoted = false;
                    _lastError = null;
                }
                RefreshStatus();
            });
        });
    }

    void OpenBrowser()
    {
        if (_config == null) return;
        var config = _config;
        ThreadPool.QueueUserWorkItem(delegate
        {
            int port = ReadWebPort(config);
            Post(delegate
            {
                try
                {
                    Process.Start("http://127.0.0.1:" + port.ToString() + "/");
                }
                catch (Exception ex)
                {
                    Balloon("管理画面を開けません", ex.Message);
                }
            });
        });
    }

    void Apply(int running, bool psOk, string psError, bool healthOk, bool recording)
    {
        _running = running;
        _healthOk = healthOk;
        _recording = recording;
        if (_config == null)
            _tip = "tray.json がありません";
        else if (healthOk)
        {
            _lastError = null;
            _upPending = false;
            _autoFailureNoted = false;
            _tip = recording ? "録画中" : "待機中";
        }
        else if (running > 0)
            _tip = "起動中";
        else if (!psOk)
            _tip = string.IsNullOrEmpty(psError) ? "Docker に接続できません" : psError;
        else if (!string.IsNullOrEmpty(_lastError))
            _tip = _lastError;
        else
            _tip = "停止中";
        ShowIcon();
        UpdateMenu();
    }

    void ShowIcon()
    {
        Icon icon = _iconStopped;
        if (_healthOk && _recording) icon = _iconRecording;
        else if (_healthOk) icon = _iconIdle;
        _notify.Icon = icon;
        _notify.Text = Tip(_tip);
    }

    void UpdateMenu()
    {
        bool have = _config != null;
        bool locked = _busy || _exitRequested;
        _start.Enabled = have && !locked;
        _stop.Enabled = have && !locked && (_healthOk || _running > 0 || _upPending);
        _rebuild.Enabled = have && !locked;
        _open.Enabled = have && !_exitRequested;
        _exit.Enabled = !_exitRequested;
    }

    void Balloon(string title, string text)
    {
        if (string.IsNullOrEmpty(text)) text = title;
        text = text.Replace("\r", "").Trim();
        if (text.Length > 200) text = text.Substring(0, 200);
        _notify.ShowBalloonTip(4000, title, text, ToolTipIcon.Error);
    }

    static string Tip(string text)
    {
        if (string.IsNullOrEmpty(text)) text = "Live Recorder";
        text = text.Replace("\r", " ").Replace("\n", " ");
        if (text.Length <= 63) return text;
        return text.Substring(0, 60) + "...";
    }

    static string FirstLine(string text)
    {
        if (string.IsNullOrEmpty(text)) return "";
        text = text.Trim();
        int n = text.IndexOf('\n');
        if (n >= 0) text = text.Substring(0, n).Trim();
        return text;
    }

    static int CountRunning(string text)
    {
        int running = 0;
        foreach (string obj in JsonObjects(text))
        {
            if (JsonRead.String(obj, "State") == "running") running++;
        }
        return running;
    }

    static List<string> JsonObjects(string text)
    {
        var list = new List<string>();
        if (string.IsNullOrEmpty(text)) return list;
        int depth = 0;
        int start = -1;
        bool str = false;
        bool esc = false;
        for (int i = 0; i < text.Length; i++)
        {
            char c = text[i];
            if (str)
            {
                if (esc) esc = false;
                else if (c == '\\') esc = true;
                else if (c == '"') str = false;
                continue;
            }
            if (c == '"') str = true;
            else if (c == '{')
            {
                if (depth == 0) start = i;
                depth++;
            }
            else if (c == '}')
            {
                depth--;
                if (depth == 0 && start >= 0)
                {
                    list.Add(text.Substring(start, i - start + 1));
                    start = -1;
                }
            }
        }
        return list;
    }

    static HealthResult FetchHealth(int port)
    {
        var down = new HealthResult();
        try
        {
            var req = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:" + port.ToString() + "/healthz");
            req.Timeout = 3000;
            req.ReadWriteTimeout = 3000;
            req.Method = "GET";
            req.Proxy = null;
            using (var res = (HttpWebResponse)req.GetResponse())
            using (var reader = new StreamReader(res.GetResponseStream(), Encoding.UTF8))
            {
                string body = reader.ReadToEnd();
                if (res.StatusCode != HttpStatusCode.OK) return down;
                if (JsonRead.String(body, "status") != "ok") return down;
                return new HealthResult { Ok = true, Recording = JsonRead.Bool(body, "recording") == true };
            }
        }
        catch (Exception)
        {
            return down;
        }
    }

    static int ReadWebPort(TrayConfig config)
    {
        return EnvPort.Parse(ReadProjectEnv(config));
    }

    static string ReadProjectEnv(TrayConfig config)
    {
        string rel = config.Project.Replace('/', '\\');
        if (rel.Length == 0 || rel[0] != '\\') rel = "\\" + rel;
        string[] roots = new string[] { @"\\wsl.localhost\", @"\\wsl$\" };
        foreach (string root in roots)
        {
            string path = root + config.Distro + rel + @"\.env";
            try
            {
                if (!File.Exists(path)) continue;
                using (var reader = new StreamReader(path, Encoding.UTF8, true))
                    return EnvPort.WebPortLines(reader);
            }
            catch (Exception)
            {
            }
        }
        var result = ExecWsl(
            config,
            "grep -E \"^[[:space:]]*(export[[:space:]]+)?WEB_PORT[[:space:]]*=\" .env",
            15000);
        if (string.IsNullOrEmpty(result.Stdout)) return "";
        return result.Stdout;
    }

    static CommandResult RunWsl(TrayConfig config, string composeArgs, int timeoutMs)
    {
        return ExecWsl(config, "docker compose " + composeArgs, timeoutMs);
    }

    static CommandResult ExecWsl(TrayConfig config, string command, int timeoutMs)
    {
        var psi = new ProcessStartInfo();
        psi.FileName = "wsl.exe";
        psi.Arguments = "-d " + Quote(config.Distro) + " --cd " + Quote(config.Project) + " -- " + command;
        psi.UseShellExecute = false;
        psi.RedirectStandardOutput = true;
        psi.RedirectStandardError = true;
        psi.CreateNoWindow = true;
        Process proc = null;
        try
        {
            proc = Process.Start(psi);
            if (proc == null) return new CommandResult { Code = -1, Stderr = "wsl.exe を起動できません" };
            byte[] stdout = new byte[0];
            byte[] stderr = new byte[0];
            var done = new ManualResetEvent(false);
            int finished = 0;
            var outThread = new Thread(new ThreadStart(delegate
            {
                try { stdout = ReadBytes(proc.StandardOutput.BaseStream); }
                catch (Exception) { }
                if (Interlocked.Increment(ref finished) == 2) done.Set();
            }));
            var errThread = new Thread(new ThreadStart(delegate
            {
                try { stderr = ReadBytes(proc.StandardError.BaseStream); }
                catch (Exception) { }
                if (Interlocked.Increment(ref finished) == 2) done.Set();
            }));
            outThread.IsBackground = true;
            errThread.IsBackground = true;
            outThread.Start();
            errThread.Start();
            if (!proc.WaitForExit(timeoutMs))
            {
                try { proc.Kill(); } catch (Exception) { }
                proc.WaitForExit();
                done.WaitOne(5000);
                return new CommandResult { Code = -1, Stdout = Decode(stdout), Stderr = "タイムアウト" };
            }
            proc.WaitForExit();
            done.WaitOne(5000);
            return new CommandResult
            {
                Code = proc.ExitCode,
                Stdout = Decode(stdout),
                Stderr = Decode(stderr),
            };
        }
        catch (Exception ex)
        {
            return new CommandResult { Code = -1, Stderr = ex.Message };
        }
        finally
        {
            if (proc != null) proc.Dispose();
        }
    }

    static byte[] ReadBytes(Stream stream)
    {
        var ms = new MemoryStream();
        var buf = new byte[4096];
        int n;
        while ((n = stream.Read(buf, 0, buf.Length)) > 0) ms.Write(buf, 0, n);
        return ms.ToArray();
    }

    static string Decode(byte[] data)
    {
        if (data == null || data.Length == 0) return "";
        if (data.Length >= 2 && data[0] == 0xFF && data[1] == 0xFE)
            return Encoding.Unicode.GetString(data, 2, data.Length - 2);
        if (LooksUtf16Le(data)) return Encoding.Unicode.GetString(data);
        return Encoding.UTF8.GetString(data);
    }

    static bool LooksUtf16Le(byte[] data)
    {
        int samples = Math.Min(data.Length / 2, 32);
        if (samples < 4) return false;
        int zeros = 0;
        for (int i = 0; i < samples; i++)
        {
            if (data[i * 2 + 1] == 0) zeros++;
        }
        return zeros > samples / 2;
    }

    static string Quote(string value)
    {
        if (string.IsNullOrEmpty(value)) return "\"\"";
        if (value.IndexOfAny(new char[] { ' ', '\t', '"' }) < 0) return value;
        return "\"" + value.Replace("\"", "\\\"") + "\"";
    }
}

static class EnvPort
{
    internal static int Parse(string text)
    {
        int port = 8080;
        if (string.IsNullOrEmpty(text)) return port;
        if (text[0] == '\uFEFF') text = text.Substring(1);
        string[] lines = text.Replace("\r", "").Split('\n');
        foreach (string raw in lines)
        {
            string line = raw.Trim();
            if (line.Length == 0 || line[0] == '#') continue;
            if (line.StartsWith("export ") || line.StartsWith("export\t"))
                line = line.Substring(7).TrimStart();
            int eq = line.IndexOf('=');
            if (eq <= 0) continue;
            if (line.Substring(0, eq).Trim() != "WEB_PORT") continue;
            string value = Value(line.Substring(eq + 1).Trim());
            int parsed;
            if (value.Length > 0
                && int.TryParse(value, NumberStyles.Integer, CultureInfo.InvariantCulture, out parsed)
                && parsed >= 1 && parsed <= 65535)
                port = parsed;
            else
                port = 8080;
        }
        return port;
    }

    internal static string WebPortLines(TextReader reader)
    {
        var sb = new StringBuilder();
        string line;
        while ((line = reader.ReadLine()) != null)
        {
            if (IsWebPortLine(line)) sb.AppendLine(line);
        }
        return sb.ToString();
    }

    static bool IsWebPortLine(string raw)
    {
        string line = raw.Trim();
        if (line.Length == 0 || line[0] == '#') return false;
        if (line.StartsWith("export ") || line.StartsWith("export\t"))
            line = line.Substring(7).TrimStart();
        int eq = line.IndexOf('=');
        if (eq <= 0) return false;
        return line.Substring(0, eq).Trim() == "WEB_PORT";
    }

    static string Value(string raw)
    {
        if (raw.Length >= 2 && (raw[0] == '"' || raw[0] == '\''))
        {
            char quote = raw[0];
            int close = raw.IndexOf(quote, 1);
            if (close >= 1) return raw.Substring(1, close - 1).Trim();
        }
        int cut = raw.IndexOf(" #");
        int tab = raw.IndexOf("\t#");
        if (tab >= 0 && (cut < 0 || tab < cut)) cut = tab;
        if (cut >= 0) raw = raw.Substring(0, cut);
        return raw.Trim();
    }
}

static class Program
{
    static Mutex _mutex;

    [STAThread]
    static void Main()
    {
        bool created = false;
        try
        {
            _mutex = new Mutex(true, @"Local\LiveRecorderTray", out created);
        }
        catch (AbandonedMutexException ex)
        {
            _mutex = ex.Mutex;
            created = true;
        }
        if (!created) return;
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        try
        {
            Application.Run(new TrayContext());
        }
        catch (Exception ex)
        {
            MessageBox.Show(ex.Message, "Live Recorder");
        }
        finally
        {
            if (_mutex != null)
            {
                _mutex.ReleaseMutex();
                _mutex.Dispose();
            }
        }
    }
}
