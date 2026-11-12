package android.content;

/** JVM-only context: serves generated fixtures, with no Android runtime. */
public final class Context {
    private final android.content.res.AssetManager assets=new android.content.res.AssetManager();
    public android.content.res.AssetManager getAssets(){return assets;}
}
