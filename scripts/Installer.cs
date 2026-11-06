using System;
using System.IO;
using System.IO.Compression;
using System.Diagnostics;
using System.Drawing;
using System.Reflection;
using System.Threading.Tasks;
using System.Windows.Forms;

// Per-user, offline installation. Payloads never include personal state or model weights.
public class Installer : Form {
    Label status;
    Button install;
    bool completed;
    const string Version="0.13.0";
    public Installer() {
        Text="安装 SCUT 学习助手";ClientSize=new Size(510,270);StartPosition=FormStartPosition.CenterScreen;
        BackColor=Color.FromArgb(250,251,248);FormBorderStyle=FormBorderStyle.FixedDialog;MaximizeBox=false;
        var title=new Label {Text="SCUT 学习助手",Font=new Font("Microsoft YaHei UI",21),AutoSize=true,Location=new Point(32,28)};
        status=new Label {Text="安装后，从桌面或开始菜单打开。\n运行环境已包含；无需安装 Python。\n不会设置开机自启。",Font=new Font("Microsoft YaHei UI",11),Location=new Point(34,88),Size=new Size(440,90)};
        install=new Button {Text="安装并打开",Font=new Font("Microsoft YaHei UI",11),Location=new Point(300,205),Size=new Size(165,40)};
        install.Click+=async(s,e)=>await Install();Controls.AddRange(new Control[]{title,status,install});
    }
    async Task Install() {
        if(completed){Close();return;}
        install.Enabled=false;status.Text="正在安装到当前用户目录…";
        try {
            string parent=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"Programs","SCUTStudy");
            string target=Path.Combine(parent,"app-"+Version);
            await Task.Run(()=>{
                Directory.CreateDirectory(parent);
                if(!File.Exists(Path.Combine(target,"SCUT学习助手.exe"))) {
                    string stage=Path.Combine(parent,"install-"+Guid.NewGuid().ToString("N"));Directory.CreateDirectory(stage);
                    using(var stream=Assembly.GetExecutingAssembly().GetManifestResourceStream("payload.zip"))
                    using(var zip=new ZipArchive(stream,ZipArchiveMode.Read))foreach(var entry in zip.Entries){
                        string path=Path.GetFullPath(Path.Combine(stage,entry.FullName));
                        if(!path.StartsWith(stage+Path.DirectorySeparatorChar,StringComparison.OrdinalIgnoreCase))throw new Exception("安装包路径无效");
                        if(String.IsNullOrEmpty(entry.Name)){Directory.CreateDirectory(path);continue;}
                        Directory.CreateDirectory(Path.GetDirectoryName(path));entry.ExtractToFile(path);
                    }
                    if(Directory.Exists(target))throw new Exception("发现未完成的安装目录，请先将该目录改名后重试："+target);
                    Directory.Move(stage,target);
                }
                var ps=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows),"System32","WindowsPowerShell","v1.0","powershell.exe");
                using(var p=Process.Start(new ProcessStartInfo(ps,"-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File \""+Path.Combine(target,"install-desktop.ps1")+"\" -NoLaunch") {UseShellExecute=false,CreateNoWindow=true,WorkingDirectory=target})){
                    if(!p.WaitForExit(60000)||p.ExitCode!=0)throw new Exception("文件已安装，但桌面入口创建失败。请打开安装目录中的 SCUT学习助手.exe。");
                }
            });
            status.Text="安装完成。以后双击桌面的 SCUT 学习助手。\n扩展与模型可在使用相关功能时再配置。";
            Process.Start(Path.Combine(target,"SCUT学习助手.exe"));install.Text="完成";completed=true;install.Enabled=true;
        }catch(Exception ex){status.Text=ex.Message;install.Enabled=true;}
    }
    [STAThread] public static void Main(){Application.EnableVisualStyles();Application.Run(new Installer());}
}
