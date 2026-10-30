"""Bounded local face grouping for review, never an identity or action grant."""
import hashlib
import json
import math

THRESHOLD = .65


def score(left, right):
    """Two independent pairs support a group despite a poor pose in one frame."""
    pairs=[]
    for i,a in enumerate(left):
        for j,b in enumerate(right):
            if len(a)!=len(b) or not a or not all(math.isfinite(x) for x in (*a,*b)):
                return -1
            denominator=math.hypot(*a)*math.hypot(*b)
            if denominator:pairs.append((sum(x*y for x,y in zip(a,b,strict=True))/denominator,i,j))
    return max(((a[0]+b[0])/2 for a in pairs for b in pairs
                if a[1]!=b[1] and a[2]!=b[2]),default=-1)


def groups(rows, *, joined=(), separate=()):
    """Complete-link groups prevent one bridging face from joining strangers.

    Rows are private dictionaries with vectors. The result deliberately contains
    no embeddings. Original candidates remain intact until explicit enrollment.
    """
    rows={r['id']:r for r in rows}
    partitions=[{key} for key in rows]
    blocked={frozenset(pair) for pair in separate}
    scores={frozenset((a,b)):score(rows[a]['vectors'],rows[b]['vectors'])
            for a in rows for b in rows if a<b}
    for members in joined:
        members=set(members)&rows.keys()
        if len(members)<2:continue
        touched=[p for p in partitions if p&members]
        union=set().union(*touched)
        partitions=[p for p in partitions if not p&members]+[union]
    for pair,value in sorted(scores.items(),key=lambda item:item[1],reverse=True):
        if value<THRESHOLD:break
        left,right=(next(p for p in partitions if key in p) for key in pair)
        if left is right or len(left|right)>8:continue
        if any(frozenset((a,b)) in blocked or scores[frozenset((a,b))]<THRESHOLD
               for a in left for b in right):continue
        partitions.remove(left);partitions.remove(right);partitions.append(left|right)
    result=[]
    for members in partitions:
        ordered=sorted(members,key=lambda key:(rows[key]['first_seen'],key))
        portrait=max(ordered,key=lambda key:rows[key]['portrait_revision'])
        revision=hashlib.sha256(json.dumps(ordered).encode()).hexdigest()
        result.append(dict(id=ordered[0],members=ordered,group_revision=revision,
            portrait_id=portrait,portrait_revision=rows[portrait]['portrait_revision'],
            first_seen=min(rows[key]['first_seen'] for key in ordered),
            last_seen=max(rows[key]['last_seen'] for key in ordered),
            observations=sum(rows[key]['observations'] for key in ordered),
            aggregation='review_group' if len(ordered)>1 else 'single'))
    return sorted(result,key=lambda row:row['last_seen'],reverse=True)
