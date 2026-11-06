package com.poesis.scutclassroom;

import android.content.Context;
import org.json.JSONArray;
import org.json.JSONObject;
import java.util.HashSet;
import java.util.Set;

final class History {
    static synchronized boolean add(Context c, JSONObject env) throws Exception {
        String id=env.getString("id"); JSONArray old=list(c); Set<String> ids=new HashSet<>();
        for(int i=0;i<old.length();i++) ids.add(old.getJSONObject(i).optString("id"));
        if(ids.contains(id)) return false;
        JSONArray fresh=new JSONArray(); fresh.put(env); for(int i=0;i<Math.min(old.length(),199);i++) fresh.put(old.getJSONObject(i));
        Pairing.prefs(c).edit().putString("history",fresh.toString()).apply(); return true;
    }
    static JSONArray list(Context c){try{return new JSONArray(Pairing.prefs(c).getString("history","[]"));}catch(Exception e){return new JSONArray();}}
    static boolean isRead(Context c,String id){try{JSONArray ids=new JSONArray(Pairing.prefs(c).getString("read_ids","[]"));for(int i=0;i<ids.length();i++)if(id.equals(ids.optString(i)))return true;}catch(Exception ignored){}return false;}
    static synchronized void setRead(Context c,String id,boolean read){JSONArray result=new JSONArray();try{JSONArray old=new JSONArray(Pairing.prefs(c).getString("read_ids","[]"));if(read)result.put(id);for(int i=0;i<Math.min(old.length(),399);i++)if(!id.equals(old.optString(i)))result.put(old.optString(i));}catch(Exception ignored){}Pairing.prefs(c).edit().putString("read_ids",result.toString()).apply();}
    static void clear(Context c){Pairing.prefs(c).edit().remove("history").remove("read_ids").apply();}
}
