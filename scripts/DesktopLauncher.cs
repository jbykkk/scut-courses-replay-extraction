using System;
using System.IO;
using System.Diagnostics;
using System.Drawing;
using System.Net;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using System.Runtime.InteropServices;

// The desktop entry owns only a tray icon. Closing a web window never stops a recording.
public class DesktopLauncher : ApplicationContext {
    static readonly string Root=AppDomain.CurrentDomain.BaseDirectory;
    static readonly string Title="SCUT 学习助手";
    static EventWaitHandle OpenEvent;
    NotifyIcon tray;
    Form splash;
    Label message;
    bool starting;
    [DllImport("user32.dll",CharSet=CharSet.Unicode)] static extern IntPtr FindWindow(string cls,string title);
    [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr window);
    [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr window,int command);
    static string StateDir() {
        var custom=Environment.GetEnvironmentVariable("SCUT_ASSISTANT_HOME");
        if(!String.IsNullOrEmpty(custom))return custom;
        return File.Exists(Path.Combine(Root,"installed.json")) ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"SCUTClassroomAssistant","data") : Path.Combine(Root,".local");
    }
    static bool Healthy() {
        try { var r=(HttpWebRequest)WebRequest.Create("http://127.0.0.1:8765/health");r.Proxy=null;r.Timeout=1600;
            using(var response=r.GetResponse())using(var reader=new StreamReader(response.GetResponseStream()))return reader.ReadToEnd().Contains("scut-local-assistant");
        } catch {return false;}
    }
    public DesktopLauncher() {
        var iconFile=Path.Combine(Root,"extension","icons","assistant.ico");
        tray=new NotifyIcon {Text=Title,Icon=File.Exists(iconFile)?new Icon(iconFile):SystemIcons.Application,Visible=true};
        var menu=new ContextMenuStrip();menu.Items.Add("打开学习助手",null,(s,e)=>Open());
        menu.Items.Add("打开百步梯",null,(s,e)=>Process.Start("https://video.jw.scut.edu.cn/"));
        menu.Items.Add("使用说明",null,(s,e)=>Process.Start("https://github.com/PoesisQ/bbt_SCUT_admin#readme"));
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("退出托盘（后台任务继续）",null,(s,e)=>{tray.Visible=false;ExitThread();});
        tray.ContextMenuStrip=menu;tray.DoubleClick+=(s,e)=>Open();
        var timer=new System.Windows.Forms.Timer {Interval=350};
        timer.Tick+=(s,e)=>{if(OpenEvent.WaitOne(0))Open();};timer.Start();
        Open();
    }
    async void Open() {
        if(starting)return;starting=true;
        try {
            if(!Healthy()) {
                splash=new Form {Text=Title,ClientSize=new Size(410,160),StartPosition=FormStartPosition.CenterScreen,FormBorderStyle=FormBorderStyle.FixedDialog,MaximizeBox=false,BackColor=Color.FromArgb(250,251,248)};
                message=new Label {Text="正在打开学习助手…\n首次启动可能需要半分钟。",AutoSize=false,Location=new Point(30,35),Size=new Size(350,75),Font=new Font("Microsoft YaHei UI",12),ForeColor=Color.FromArgb(50,75,64)};
                splash.Controls.Add(message);splash.Show();
                await Task.Run(()=>{
                    var psi=new ProcessStartInfo(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows),"System32","WindowsPowerShell","v1.0","powershell.exe"),"-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File \""+Path.Combine(Root,"start-assistant.ps1")+"\" -NoBrowser") {WorkingDirectory=Root,UseShellExecute=false,CreateNoWindow=true};
                    using(var p=Process.Start(psi)){if(!p.WaitForExit(115000)||p.ExitCode!=0)throw new Exception("本地服务没有正常启动。启动日志："+Path.Combine(StateDir(),"launcher.log"));}
                    if(!Healthy())throw new Exception("本地服务还未响应，请稍后从桌面重试。日志："+Path.Combine(StateDir(),"server-error.log"));
                });
            }
            var handle=FindWindow(null,"学习伙伴 · SCUT");
            if(handle!=IntPtr.Zero){ShowWindow(handle,9);SetForegroundWindow(handle);return;}
            var url="http://127.0.0.1:8765/"+(File.Exists(Path.Combine(StateDir(),"desktop-welcomed"))?"study.html":"start.html");
            string edge=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86),"Microsoft","Edge","Application","msedge.exe");
            if(!File.Exists(edge))edge=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),"Microsoft","Edge","Application","msedge.exe");
            if(File.Exists(edge))Process.Start(new ProcessStartInfo(edge,"--app="+url) {UseShellExecute=true});
            else Process.Start(url);
        }catch(Exception ex){MessageBox.Show(ex.Message,Title,MessageBoxButtons.OK,MessageBoxIcon.Information);}
        finally{if(splash!=null){splash.Close();splash.Dispose();splash=null;}starting=false;}
    }
    [STAThread] public static void Main() {
        bool first;
        using(var mutex=new Mutex(true,"Local\\SCUTLearningDesktop",out first)) {
            OpenEvent=new EventWaitHandle(false,EventResetMode.AutoReset,"Local\\SCUTLearningDesktopOpen");
            if(!first){OpenEvent.Set();return;}
            Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new DesktopLauncher());OpenEvent.Dispose();
        }
    }
}
