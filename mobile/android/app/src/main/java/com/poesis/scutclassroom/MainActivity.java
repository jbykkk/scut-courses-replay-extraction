package com.poesis.scutclassroom;

import android.Manifest;
import android.app.*;
import android.content.*;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.os.*;
import android.text.InputType;
import android.text.Editable;
import android.text.TextWatcher;
import android.view.*;
import android.widget.*;
import com.google.zxing.integration.android.IntentIntegrator;
import com.google.zxing.integration.android.IntentResult;
import org.json.*;
import java.text.SimpleDateFormat;
import java.util.*;

public class MainActivity extends Activity {
    LinearLayout root, history;
    TextView state, countView, syncView, errorView, connectionBadge, emptyTitle;
    EditText code;
    Switch enabled;
    boolean showPairing;
    String historySnapshot;
    String filter="all", searchText="";
    final Handler ui = new Handler(Looper.getMainLooper());
    final Runnable refreshLoop = new Runnable() {
        @Override public void run() { refreshDashboard(); ui.postDelayed(this, 2000); }
    };
    final int blue=Color.rgb(42,91,214), ink=Color.rgb(29,32,43), muted=Color.rgb(117,123,143);
    final int paper=Color.rgb(248,248,251), line=Color.rgb(229,231,238), green=Color.rgb(52,139,102);

    @Override public void onCreate(Bundle b) {
        super.onCreate(b);
        if(Build.VERSION.SDK_INT>=33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)!=PackageManager.PERMISSION_GRANTED)
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS},40);
        render();
    }

    @Override protected void onResume() { super.onResume(); if(Pairing.load(this)!=null && Pairing.prefs(this).getBoolean("enabled",false))startForegroundService(new Intent(this,RelayService.class)); ui.removeCallbacks(refreshLoop); ui.post(refreshLoop); }
    @Override protected void onPause() { ui.removeCallbacks(refreshLoop); super.onPause(); }

    void render() {
        history=null; state=null; emptyTitle=null; historySnapshot=null;
        ScrollView scroll=new ScrollView(this);
        scroll.setFillViewport(true);
        root=new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(dp(22),dp(28),dp(22),dp(44));
        root.setBackgroundColor(paper);
        scroll.addView(root);
        setContentView(scroll);
        Pairing pairing=Pairing.load(this);
        brand(pairing!=null);
        if(pairing==null) renderWelcome(); else renderCenter();
    }

    void brand(boolean paired) {
        LinearLayout row=new LinearLayout(this); row.setGravity(Gravity.CENTER_VERTICAL);
        ImageView logo=new ImageView(this); logo.setImageResource(R.drawable.classroom_mark); logo.setScaleType(ImageView.ScaleType.CENTER_INSIDE);
        row.addView(logo,new LinearLayout.LayoutParams(dp(46),dp(46)));
        LinearLayout words=new LinearLayout(this); words.setOrientation(LinearLayout.VERTICAL); words.setPadding(dp(10),0,0,0);
        TextView name=text("课堂提醒",18,ink); name.setTypeface(Typeface.DEFAULT,Typeface.BOLD); words.addView(name);
        TextView sub=text("SCUT",9,muted); sub.setLetterSpacing(.16f); words.addView(sub,margin(0,2,0,0));
        row.addView(words,new LinearLayout.LayoutParams(0,-2,1));
        TextView badge=text(paired?"已配对":"待配对",11,paired?green:muted); connectionBadge=badge; badge.setGravity(Gravity.CENTER);
        badge.setBackground(round(paired?Color.rgb(234,248,240):Color.rgb(238,240,245),99,0,Color.TRANSPARENT));
        row.addView(badge,new LinearLayout.LayoutParams(dp(70),dp(34)));
        root.addView(row);
    }

    void renderWelcome() {
        TextView mark=text("SCUT",10,blue); mark.setLetterSpacing(.20f); root.addView(mark,margin(0,42,0,0));
        TextView title=text("连接课堂提醒",33,ink); title.setTypeface(Typeface.DEFAULT,Typeface.BOLD); title.setLineSpacing(0,.96f); root.addView(title,margin(0,14,0,0));
        root.addView(text("接收电脑发来的课堂事项和复习提醒。",15,muted),margin(0,16,0,28));
        root.addView(pairingCard("连接电脑", "扫描电脑上的私人配对码，提醒只在你的设备上解密。"));
    }

    void renderCenter() {
        TextView mark=text("SCUT",10,blue); mark.setLetterSpacing(.20f); root.addView(mark,margin(0,38,0,0));
        TextView title=text("收件箱",30,ink); title.setTypeface(Typeface.DEFAULT,Typeface.BOLD); root.addView(title,margin(0,9,0,0));
        root.addView(text("课堂事项和复习分开查看。离线时仍可搜索本机记录。",14,muted),margin(0,8,0,20));

        LinearLayout status=card(Color.WHITE);
        LinearLayout top=new LinearLayout(this); top.setGravity(Gravity.CENTER_VERTICAL);
        View dot=new View(this); dot.setBackground(round(Color.rgb(47,194,125),99,0,Color.TRANSPARENT)); top.addView(dot,new LinearLayout.LayoutParams(dp(9),dp(9)));
        state=text("正在读取连接状态",14,ink); state.setTypeface(Typeface.DEFAULT,Typeface.BOLD); top.addView(state,marginWeight(10,0,0,0,1));
        enabled=new Switch(this); top.addView(enabled,new LinearLayout.LayoutParams(-2,-2)); status.addView(top);
        View divider=new View(this); divider.setBackgroundColor(line); status.addView(divider,marginHeight(0,17,0,16,1));
        LinearLayout metrics=new LinearLayout(this); metrics.setOrientation(LinearLayout.HORIZONTAL);
        countView=metric("0", "本机提醒"); syncView=metric("等待同步", "最近连接");
        metrics.addView(countView,new LinearLayout.LayoutParams(0,-2,1)); metrics.addView(syncView,new LinearLayout.LayoutParams(0,-2,1)); status.addView(metrics);
        errorView=text("",12,Color.rgb(184,67,69)); errorView.setVisibility(View.GONE); status.addView(errorView,margin(0,13,0,0));
        Button reconnect=linkButton("立即重试连接"); status.addView(reconnect,marginHeight(0,8,0,0,42));
        reconnect.setOnClickListener(v->{Pairing.prefs(this).edit().putBoolean("enabled",true).apply();startForegroundService(new Intent(this,RelayService.class).setAction(RelayService.RETRY));refreshDashboard();});
        root.addView(status);

        boolean on=Pairing.prefs(this).getBoolean("enabled",false); enabled.setChecked(on);
        enabled.setOnCheckedChangeListener((v,w)->{
            Pairing.prefs(this).edit().putBoolean("enabled",w).apply();
            if(w) startForegroundService(new Intent(this,RelayService.class));
            else startService(new Intent(this,RelayService.class).setAction(RelayService.STOP));
            refreshDashboard();
        });

        LinearLayout heading=new LinearLayout(this); heading.setGravity(Gravity.CENTER_VERTICAL);
        TextView recent=text("最近收到",21,ink); recent.setTypeface(Typeface.DEFAULT,Typeface.BOLD); heading.addView(recent,new LinearLayout.LayoutParams(0,-2,1));
        Button clear=linkButton("清空"); heading.addView(clear,new LinearLayout.LayoutParams(-2,dp(42))); root.addView(heading,margin(0,27,0,2));
        clear.setOnClickListener(v->new AlertDialog.Builder(this).setTitle("清空本机提醒？").setMessage("只删除手机上的缓存，不影响电脑课程笔记。")
            .setNegativeButton("取消",null).setPositiveButton("清空",(d,w)->{History.clear(this);refreshDashboard();}).show());
        HorizontalScrollView choices=new HorizontalScrollView(this); choices.setHorizontalScrollBarEnabled(false);
        LinearLayout chips=new LinearLayout(this);
        String[] keys={"all","unread","class","review","assignment"}, titles={"全部","未读","课堂","复习","作业 / 测验"};
        for(int k=0;k<keys.length;k++){final String selected=keys[k];Button b=button(titles[k],false);b.setTextColor(filter.equals(selected)?blue:muted);chips.addView(b,new LinearLayout.LayoutParams(-2,dp(44)));b.setOnClickListener(v->{filter=selected;historySnapshot=null;refreshDashboard();for(int j=0;j<chips.getChildCount();j++)((Button)chips.getChildAt(j)).setTextColor(chips.getChildAt(j)==v?blue:muted);});}
        choices.addView(chips);root.addView(choices,margin(0,5,0,10));
        EditText search=new EditText(this);search.setSingleLine(true);search.setTextSize(14);search.setHint("搜索课程、事项、复习内容");search.setText(searchText);search.setPadding(dp(14),0,dp(14),0);search.setBackground(round(Color.WHITE,14,1,line));root.addView(search,marginHeight(0,0,0,8,48));
        search.addTextChangedListener(new TextWatcher(){public void beforeTextChanged(CharSequence s,int a,int c,int f){}public void onTextChanged(CharSequence s,int a,int b,int c){searchText=s.toString();historySnapshot=null;refreshDashboard();}public void afterTextChanged(Editable e){}});
        history=new LinearLayout(this); history.setOrientation(LinearLayout.VERTICAL); root.addView(history);

        LinearLayout tools=card(Color.rgb(251,248,246));
        TextView toolsTitle=text("连接与隐私",15,ink); toolsTitle.setTypeface(Typeface.DEFAULT,Typeface.BOLD); tools.addView(toolsTitle);
        tools.addView(text("提醒密钥仅保存在本机。更换电脑或重新生成二维码时，再进行配对。",12,muted),margin(0,7,0,12));
        Button repair=button(showPairing?"收起配对工具":"更换配对",false); tools.addView(repair,new LinearLayout.LayoutParams(-1,dp(46)));
        repair.setOnClickListener(v->{showPairing=!showPairing;render();}); root.addView(tools,margin(0,26,0,0));
        if(showPairing) root.addView(pairingCard("重新配对", "新配对会从对应通道开始同步，现有本机记录仍会保留。"),margin(0,12,0,0));
        refreshDashboard();
    }

    LinearLayout pairingCard(String heading,String copy) {
        LinearLayout pair=card(Color.WHITE);
        TextView pairTitle=text(heading,20,ink); pairTitle.setTypeface(Typeface.DEFAULT,Typeface.BOLD); pair.addView(pairTitle);
        pair.addView(text(copy,13,muted),margin(0,8,0,17));
        LinearLayout buttons=new LinearLayout(this); buttons.setOrientation(LinearLayout.HORIZONTAL);
        Button scan=button("扫描二维码",true),paste=button("从剪贴板粘贴",false);
        buttons.addView(scan,new LinearLayout.LayoutParams(0,dp(50),1)); LinearLayout.LayoutParams p2=new LinearLayout.LayoutParams(0,dp(50),1);p2.setMarginStart(dp(10));buttons.addView(paste,p2);pair.addView(buttons);
        code=new EditText(this); code.setHint("粘贴 SCUT1. 开头的配对码"); code.setTextSize(12); code.setSingleLine(false); code.setMinLines(2); code.setPadding(0,dp(12),0,dp(8));
        code.setInputType(InputType.TYPE_CLASS_TEXT|InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS); pair.addView(code,margin(0,10,0,0));
        Button save=button("保存并开始监听",true); pair.addView(save,marginHeight(0,11,0,0,52));
        scan.setOnClickListener(v->new IntentIntegrator(this).setPrompt("扫描电脑上的课堂提醒配对码").setBeepEnabled(false).setOrientationLocked(false).initiateScan());
        paste.setOnClickListener(v->{ClipboardManager c=(ClipboardManager)getSystemService(CLIPBOARD_SERVICE);if(c.hasPrimaryClip())code.setText(c.getPrimaryClip().getItemAt(0).coerceToText(this));});
        save.setOnClickListener(v->pair(String.valueOf(code.getText())));
        return pair;
    }

    void pair(String raw) {
        try {
            Pairing p=new Pairing(raw); p.save(this); showPairing=false;
            startForegroundService(new Intent(this,RelayService.class));
            Toast.makeText(this,"配对已保存，正在连接课堂信箱",Toast.LENGTH_LONG).show(); render();
        } catch(Exception e) { Toast.makeText(this,e.getMessage(),Toast.LENGTH_LONG).show(); }
    }

    void refreshDashboard() {
        if(history==null || state==null) return;
        boolean on=Pairing.prefs(this).getBoolean("enabled",false);
        JSONArray rows=History.list(this);
        if(enabled!=null && enabled.isChecked()!=on) enabled.setChecked(on);
        countView.setText(String.format(Locale.CHINA,"%d 条\n本机提醒",rows.length()));
        long last=Pairing.prefs(this).getLong("last_sync",0);
        syncView.setText(String.format(Locale.CHINA,"%s\n最近连接",last==0?"连接中":clock(last)));
        String err=Pairing.prefs(this).getString("last_error","");
        boolean connected=on && err.isEmpty() && last>0 && System.currentTimeMillis()-last<90000;
        boolean catchingUp=Pairing.prefs(this).getBoolean("catching_up",false);
        state.setText(!on?"提醒已暂停":connected?(catchingUp?"正在补齐历史提醒":"课堂信箱已连接"):!err.isEmpty()?"正在重新连接":"正在连接课堂信箱");
        connectionBadge.setText(!on?"已暂停":connected?"已连接":"连接中");
        connectionBadge.setTextColor(connected?green:muted);
        errorView.setText(on?err:""); errorView.setVisibility(on&&!err.isEmpty()?View.VISIBLE:View.GONE);
        String snapshot=rows.toString()+filter+searchText+Pairing.prefs(this).getString("read_ids","[]");
        if(!snapshot.equals(historySnapshot)){historySnapshot=snapshot;renderHistory(rows);}
        if(emptyTitle!=null)emptyTitle.setText(connected?"等待下一条课堂提醒":!on?"提醒已暂停":"正在连接，已有记录会保留");
    }

    void renderHistory(JSONArray rows) {
        history.removeAllViews(); emptyTitle=null;
        if(rows.length()==0) {
            LinearLayout empty=card(Color.WHITE); ImageView art=new ImageView(this); art.setImageResource(R.drawable.classroom_mark); art.setAlpha(.82f); art.setScaleType(ImageView.ScaleType.CENTER_INSIDE); empty.addView(art,new LinearLayout.LayoutParams(-1,dp(88)));
            TextView title=text("正在连接课堂信箱",16,ink);emptyTitle=title;title.setTypeface(Typeface.DEFAULT,Typeface.BOLD);title.setGravity(Gravity.CENTER);empty.addView(title,margin(0,8,0,0));
            TextView desc=text("收到的信息会自动出现在这里，无需重新打开 App。",12,muted);desc.setGravity(Gravity.CENTER);empty.addView(desc,margin(0,7,0,0)); history.addView(empty,margin(0,12,0,0)); return;
        }
        ArrayList<JSONObject> ordered=new ArrayList<>();
        for(int n=0;n<rows.length();n++){JSONObject e=rows.optJSONObject(n);if(e!=null&&e.optJSONObject("d")!=null)ordered.add(e);}
        ordered.sort((a,b)->Long.compare(eventTime(b,b.optJSONObject("d")),eventTime(a,a.optJSONObject("d"))));
        int shown=0;
        for(int i=0;i<Math.min(ordered.size(),200);i++) {
            try {
                JSONObject env=ordered.get(i), d=env.getJSONObject("d");
                String id=env.optString("id"),category=d.optString("category"),source=d.optString("source");boolean isReview="study-review".equals(source),read=History.isRead(this,id);
                if((filter.equals("unread")&&read)||(filter.equals("review")&&!isReview)||(filter.equals("class")&&isReview)||(filter.equals("assignment")&&!category.equals("assignment")&&!category.equals("quiz")))continue;
                String haystack=(d.optString("course")+" "+d.optString("lesson")+" "+d.optString("message")+" "+details(d.optJSONObject("details"))).toLowerCase(Locale.ROOT);
                if(!haystack.contains(searchText.toLowerCase(Locale.ROOT)))continue;
                shown++;int accent=isReview?green:categoryColor(category);
                LinearLayout item=card(Color.WHITE);
                LinearLayout top=new LinearLayout(this); top.setGravity(Gravity.CENTER_VERTICAL);
                TextView tag=text(isReview?"复习":categoryName(d),11,accent); tag.setGravity(Gravity.CENTER); tag.setTypeface(Typeface.DEFAULT,Typeface.BOLD); tag.setBackground(round(tint(accent),99,0,Color.TRANSPARENT)); top.addView(tag,new LinearLayout.LayoutParams(dp(72),dp(30)));
                long when=eventTime(env,d); TextView time=text(timeLabel(when),12,ink); time.setGravity(Gravity.END); time.setTypeface(Typeface.DEFAULT,Typeface.BOLD); top.addView(time,new LinearLayout.LayoutParams(0,-2,1)); item.addView(top);
                String course=d.optString("course","当前课程"),lesson=d.optString("lesson",""); TextView courseView=text(course+(lesson.isEmpty()?"":"  ·  "+lesson),12,muted); item.addView(courseView,margin(0,13,0,0));
                TextView msg=text(d.optString("message","请查看课堂信息"),17,ink); msg.setTypeface(Typeface.DEFAULT,Typeface.BOLD); item.addView(msg,margin(0,6,0,0));msg.setTextIsSelectable(true);
                String detail=details(d.optJSONObject("details")); if(!detail.isEmpty()) item.addView(text(detail,13,muted),margin(0,10,0,0));
                String classAt=isReview?"":classTime(d.optDouble("start",-1)); if(!classAt.isEmpty()) item.addView(text("课堂时间  "+classAt,11,muted),margin(0,12,0,0));
                if(isReview){int minutes=d.optJSONObject("details")==null?5:d.optJSONObject("details").optInt("estimated_minutes",5);item.addView(text("预计 "+minutes+" 分钟 · 作答和调整时间请回电脑学习页",12,muted),margin(0,10,0,0));}
                Button mark=linkButton(read?"已读 · 设为未读":"标为已读");mark.setOnClickListener(v->{History.setRead(this,id,!read);historySnapshot=null;refreshDashboard();});item.addView(mark,marginHeight(0,10,0,0,40));
                history.addView(item,margin(0,12,0,0));
            } catch(Exception ignored) {}
        }
        if(shown==0)history.addView(text("没有符合筛选的记录",14,muted),margin(0,20,0,10));
    }

    String categoryName(JSONObject d) {
        String label=d.optString("label",""); if(!label.isEmpty()) return label.length()>6?label.substring(0,6):label;
        switch(d.optString("category")){case "attendance":return "点名签到";case "qr":return "扫码";case "assignment":return "作业";case "quiz":return "测验";case "schedule":return "课程安排";case "question":return "课堂提问";case "grading":return "成绩";default:return "重要提醒";}
    }
    int categoryColor(String c){switch(c){case "attendance":case "qr":return Color.rgb(211,76,86);case "assignment":return Color.rgb(126,75,190);case "quiz":case "question":return Color.rgb(220,116,45);case "schedule":return Color.rgb(38,126,171);case "grading":return Color.rgb(176,57,125);default:return blue;}}
    int tint(int c){return Color.rgb((Color.red(c)+255*5)/6,(Color.green(c)+255*5)/6,(Color.blue(c)+255*5)/6);}
    long eventTime(JSONObject env,JSONObject d){double happened=d.optDouble("occurred_at",0);return happened>0?(long)(happened*1000):env.optLong("ts",System.currentTimeMillis());}
    String timeLabel(long ms){Calendar a=Calendar.getInstance(),b=Calendar.getInstance();b.setTimeInMillis(ms);String pattern=a.get(Calendar.YEAR)==b.get(Calendar.YEAR)&&a.get(Calendar.DAY_OF_YEAR)==b.get(Calendar.DAY_OF_YEAR)?"今天 HH:mm:ss":"MM月dd日 HH:mm";return new SimpleDateFormat(pattern,Locale.CHINA).format(new Date(ms));}
    String clock(long ms){return new SimpleDateFormat("HH:mm:ss",Locale.CHINA).format(new Date(ms));}
    String classTime(double sec){if(sec<0)return "";int s=(int)Math.round(sec);return String.format(Locale.CHINA,"%02d:%02d",s/60,s%60);}
    String details(JSONObject d){if(d==null)return "";StringBuilder s=new StringBuilder();for(String k:new String[]{"action","deadline","submission","requirements","grading","location","response","score"}){String v=d.optString(k);if(!v.isEmpty()&&!s.toString().contains(v)){if(s.length()>0)s.append("\n");s.append(v);}}return s.toString();}

    @Override protected void onActivityResult(int request,int result,Intent data){IntentResult r=IntentIntegrator.parseActivityResult(request,result,data);if(r!=null){if(r.getContents()!=null)pair(r.getContents());return;}super.onActivityResult(request,result,data);}
    TextView text(String value,int size,int color){TextView v=new TextView(this);v.setText(value);v.setTextSize(size);v.setTextColor(color);v.setLineSpacing(dp(2),1.08f);return v;}
    TextView metric(String value,String label){TextView v=text(value+"\n"+label,12,muted);v.setTypeface(Typeface.DEFAULT,Typeface.BOLD);v.setLineSpacing(dp(4),1);return v;}
    LinearLayout card(int color){LinearLayout v=new LinearLayout(this);v.setOrientation(LinearLayout.VERTICAL);v.setPadding(dp(20),dp(20),dp(20),dp(20));v.setBackground(round(color,24,1,line));v.setElevation(dp(1));return v;}
    GradientDrawable round(int color,int radius,int stroke,int strokeColor){GradientDrawable g=new GradientDrawable();g.setColor(color);g.setCornerRadius(dp(radius));if(stroke>0)g.setStroke(dp(stroke),strokeColor);return g;}
    Button button(String value,boolean primary){Button b=new Button(this);b.setText(value);b.setTextSize(13);b.setTypeface(Typeface.DEFAULT,Typeface.BOLD);b.setAllCaps(false);b.setTextColor(primary?Color.WHITE:ink);b.setBackground(round(primary?blue:Color.rgb(239,241,247),15,0,Color.TRANSPARENT));return b;}
    Button linkButton(String value){Button b=button(value,false);b.setTextColor(muted);b.setBackgroundColor(Color.TRANSPARENT);return b;}
    LinearLayout.LayoutParams margin(int l,int t,int r,int b){LinearLayout.LayoutParams p=new LinearLayout.LayoutParams(-1,-2);p.setMargins(dp(l),dp(t),dp(r),dp(b));return p;}
    LinearLayout.LayoutParams marginWeight(int l,int t,int r,int b,float w){LinearLayout.LayoutParams p=new LinearLayout.LayoutParams(0,-2,w);p.setMargins(dp(l),dp(t),dp(r),dp(b));return p;}
    LinearLayout.LayoutParams marginHeight(int l,int t,int r,int b,int h){LinearLayout.LayoutParams p=new LinearLayout.LayoutParams(-1,dp(h));p.setMargins(dp(l),dp(t),dp(r),dp(b));return p;}
    int dp(int n){return Math.round(n*getResources().getDisplayMetrics().density);}
}
