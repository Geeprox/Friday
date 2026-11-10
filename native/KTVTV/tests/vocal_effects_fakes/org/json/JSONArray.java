package org.json;
public final class JSONArray {
 private final java.util.List<Object> values=new java.util.ArrayList<>();
 public JSONArray put(Object value){values.add(value);return this;}
 public int length(){return values.size();}
}
