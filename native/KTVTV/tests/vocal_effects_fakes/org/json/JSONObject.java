package org.json;
/** Test transport envelopes use opaque map handles, not a replacement JSON
 * parser. These tests exercise the production controller, not serialization. */
public final class JSONObject {
 private static final java.util.Map<String,JSONObject> envelopes=new java.util.HashMap<>();
 private final java.util.Map<String,Object> values=new java.util.HashMap<>();
 public JSONObject(){}
 public JSONObject(String value)throws JSONException{JSONObject stored=envelopes.get(value);if(stored==null)throw new JSONException("unknown test envelope");values.putAll(stored.values);}
 public JSONObject put(String key,Object value)throws JSONException{values.put(key,value);return this;}
 public JSONObject optJSONObject(String key){Object value=values.get(key);return value instanceof JSONObject?(JSONObject)value:null;}
 public String optString(String key){return optString(key,"");}
 public String optString(String key,String fallback){Object value=values.get(key);return value instanceof String?(String)value:fallback;}
 public boolean optBoolean(String key){return Boolean.TRUE.equals(values.get(key));}
 public int optInt(String key){return optInt(key,0);}
 public int optInt(String key,int fallback){Object value=values.get(key);return value instanceof Number?((Number)value).intValue():fallback;}
 public long optLong(String key){return optLong(key,0);}
 public long optLong(String key,long fallback){Object value=values.get(key);return value instanceof Number?((Number)value).longValue():fallback;}
 public String toString(){String key="test-envelope-"+envelopes.size();envelopes.put(key,this);return key;}
}
