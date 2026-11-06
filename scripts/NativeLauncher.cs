using System;
using System.IO;
using System.Net;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Collections.Generic;
using System.Web.Script.Serialization;

// Fixed-purpose native messaging host. No caller-supplied paths or commands.
public static class NativeLauncher {
    static readonly JavaScriptSerializer Json = new JavaScriptSerializer();
    static string Root;
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
    struct StartupInfo { public int cb; public string reserved,desktop,title; public int x,y,xsize,ysize,xchars,ychars,fill,flags; public short show,reserved2; public IntPtr reservedPtr,input,output,error; }
    [StructLayout(LayoutKind.Sequential)] struct ProcessInfo { public IntPtr process,thread; public int pid,tid; }
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
    static extern bool CreateProcess(string app,StringBuilder command,IntPtr pa,IntPtr ta,bool inherit,uint flags,IntPtr env,string cwd,ref StartupInfo startup,out ProcessInfo info);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);

    static Dictionary<string,object> ReadJson(string path) { return Json.Deserialize<Dictionary<string,object>>(File.ReadAllText(path,Encoding.UTF8)); }
    static Dictionary<string,object> Health(string token=null) {
        try {
            var request=(HttpWebRequest)WebRequest.Create("http://127.0.0.1:8765/"+(token==null?"health":"api/health?brief=true"));
            request.Proxy=null; request.Timeout=1500; request.ReadWriteTimeout=1500;
            if(token!=null)request.Headers["Authorization"]="Bearer "+token;
            using(var response=request.GetResponse())using(var reader=new StreamReader(response.GetResponseStream()))
                return Json.Deserialize<Dictionary<string,object>>(reader.ReadToEnd());
        } catch(WebException) { return null; }
    }
    static void Launch() {
        string powershell=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows),"System32","WindowsPowerShell","v1.0","powershell.exe");
        string script=Path.Combine(Root,"start-assistant.ps1");
        if(!File.Exists(script))throw new Exception("本地启动器不存在，请重新安装项目启动器");
        string command="\""+powershell+"\" -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File \""+script+"\" -NoBrowser";
        var si=new StartupInfo();si.cb=Marshal.SizeOf(si);si.flags=1;si.show=0;ProcessInfo pi;
        // Detach the service launcher from the browser host's lifetime/job when supported.
        bool created=CreateProcess(powershell,new StringBuilder(command),IntPtr.Zero,IntPtr.Zero,false,0x08000000|0x01000000,IntPtr.Zero,Root,ref si,out pi);
        if(!created && Marshal.GetLastWin32Error()==5)
            created=CreateProcess(powershell,new StringBuilder(command),IntPtr.Zero,IntPtr.Zero,false,0x08000000,IntPtr.Zero,Root,ref si,out pi);
        if(!created)throw new Exception("Windows 无法启动本地服务，错误 "+Marshal.GetLastWin32Error());
        CloseHandle(pi.thread);CloseHandle(pi.process);
    }
    static object Handle(Dictionary<string,object> message) {
        object action;if(!message.TryGetValue("action",out action) || !(action is string) || ((string)action!="start" && (string)action!="status"))
            throw new Exception("不支持的启动操作");
        var health=Health();
        if(health==null && (string)action=="start") {
            Launch();var timer=Stopwatch.StartNew();
            while(timer.Elapsed.TotalSeconds<80 && health==null){Thread.Sleep(400);health=Health();}
        }
        if(health==null)return new {ok=false,error="服务尚未就绪，请查看 .local/launcher.log 中的启动原因"};
        if(!health.ContainsKey("app") || (string)health["app"]!="scut-local-assistant")throw new Exception("本地端口被其他程序占用");
        string stateRoot=Environment.GetEnvironmentVariable("SCUT_ASSISTANT_HOME");
        if(String.IsNullOrEmpty(stateRoot))stateRoot=File.Exists(Path.Combine(Root,"installed.json"))?Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"SCUTClassroomAssistant","data"):Path.Combine(Root,".local");
        var settings=ReadJson(Path.Combine(stateRoot,"settings.json"));string token=(string)settings["token"];
        if(Health(token)==null)throw new Exception("服务与本机连接配置不一致，请检查是否启动了另一份项目");
        return new {ok=true,token=token,version=health["version"]};
    }
    static byte[] ReadExactly(Stream stream,int size) {
        var data=new byte[size];int offset=0,n;
        while(offset<size){n=stream.Read(data,offset,size-offset);if(n==0)throw new EndOfStreamException();offset+=n;}
        return data;
    }
    public static void Main(string[] args) {
        object result;
        try {
            string directory=Path.GetDirectoryName(typeof(NativeLauncher).Assembly.Location);
            var config=ReadJson(Path.Combine(directory,"native-launcher-config.json"));
            var origins=Json.ConvertToType<string[]>(config["origins"]);
            if(args.Length<1 || Array.IndexOf(origins,args[0])<0)throw new Exception("扩展未获本地启动授权");
            Root=(string)config["root"];
            var input=Console.OpenStandardInput();int length=BitConverter.ToInt32(ReadExactly(input,4),0);
            if(length<2 || length>4096)throw new Exception("无效启动消息");
            var message=Json.Deserialize<Dictionary<string,object>>(Encoding.UTF8.GetString(ReadExactly(input,length)));
            result=Handle(message);
        } catch(EndOfStreamException) { return; }
        catch(Exception error) { result=new {ok=false,error=error is IOException?"本地启动配置无法读取，请运行一次项目启动器":error.Message}; }
        var output=Console.OpenStandardOutput();byte[] data=Encoding.UTF8.GetBytes(Json.Serialize(result));
        output.Write(BitConverter.GetBytes(data.Length),0,4);output.Write(data,0,data.Length);output.Flush();
    }
}
