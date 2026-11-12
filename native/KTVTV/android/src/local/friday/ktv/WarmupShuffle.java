package local.friday.ktv;

import java.util.*;

/** A changing catalog does not restart the round or repeat its most recent song. */
public final class WarmupShuffle {
    private final Random random;
    private final List<String> remaining=new ArrayList<>();
    private final Set<String> seen=new HashSet<>();
    private String last="";
    public WarmupShuffle(){this(new Random());}
    public WarmupShuffle(Random random){this.random=random;}
    public String next(Collection<String> candidates){
        Set<String> available=new LinkedHashSet<>(candidates);
        available.remove("");
        remaining.retainAll(available);seen.retainAll(available);
        for(String key:available)if(!seen.contains(key)&&!remaining.contains(key))remaining.add(random.nextInt(remaining.size()+1),key);
        if(remaining.isEmpty()){
            seen.clear();remaining.addAll(available);Collections.shuffle(remaining,random);
        }
        if(remaining.isEmpty())return null;
        int at=0;
        if(remaining.size()>1&&remaining.get(0).equals(last))at=1;
        last=remaining.remove(at);seen.add(last);return last;
    }
}
