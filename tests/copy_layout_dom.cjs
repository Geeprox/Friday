'use strict';
// Small DOM interfaces needed by the actual adjacent-copy implementation.
// Fixture-only: no browser, network or provider behavior is replaced here.
function install(Element, document, root) {
  document.body ??= root;
  document.documentElement ??= document.body;
  document.__copyObservers ??= [];
  const create = document.createElement;
  document.createElement = tag => {const node=create(tag);node.ownerDocument=document;return node;};
  document.body.ownerDocument = document;
  const walk = node => node.children.flatMap(child => [child,...walk(child)]);
  document.querySelectorAll = selector => document.body.querySelectorAll(selector);
  if (!Object.prototype.hasOwnProperty.call(Element.prototype,'__copyDOM')) {
    Element.prototype.__copyDOM = true;
    const proto=Element.prototype, append=proto.append, replace=proto.replaceChildren;
    const changed = node => {for(const callback of node.ownerDocument?.__copyObservers??[])queueMicrotask(callback);};
    if (!root.tagName) Object.defineProperty(proto,'tagName',{get(){return this.tag.toUpperCase();},configurable:true});
    const text=Object.getOwnPropertyDescriptor(proto,'textContent');
    Object.defineProperty(proto,'textContent',{get:text.get,set(v){for(const child of this.children){child.parentNode=null;child.parent=null;}text.set.call(this,v);changed(this);},configurable:true});
    Object.defineProperty(proto,'parentElement',{get(){return this.parentNode??this.parent??null;}});
    Object.defineProperty(proto,'nextSibling',{get(){const p=this.parentElement;return p?.children[p.children.indexOf(this)+1]??null;}});
    Object.defineProperty(proto,'style',{get(){return this.__style??= {setProperty(k,v){this[k]=String(v);},removeProperty(k){delete this[k];}};}});
    Object.defineProperty(proto,'className',{get(){return this.getAttribute('class')??'';},set(v){this.setAttribute('class',v);}});
    Object.defineProperty(proto,'id',{get(){return this.getAttribute('id')??'';},set(v){this.setAttribute('id',v);}});
    if (!Object.getOwnPropertyDescriptor(proto,'isConnected') && !Object.hasOwn(root,'isConnected')) Object.defineProperty(proto,'isConnected',{
      get(){return this===this.ownerDocument?.body||!!this.parentElement?.isConnected;}});
    proto.getAttribute ??= function(k){return (this.attrs??this.attributes)[k]??null;};
    proto.removeAttribute ??= function(k){delete (this.attrs??this.attributes)[k];};
    proto.remove=function(){const p=this.parentElement;if(p){p.children=p.children.filter(n=>n!==this);this.parentNode=null;this.parent=null;changed(p);}};
    proto.append=function(...children){for(const child of children){child.remove();child.parentNode=this;}append.call(this,...children);changed(this);};
    proto.replaceChildren=function(...children){for(const child of this.children){child.parentNode=null;child.parent=null;}replace.call(this,...children);for(const child of children)child.parentNode=this;changed(this);};
    proto.insertBefore=function(child,before){child.remove();const index=before?this.children.indexOf(before):-1;child.parentNode=this;child.parent=this;if(index<0)this.children.push(child);else this.children.splice(index,0,child);changed(this);return child;};
    proto.closest=function(selector){const tags=selector.split(',').map(t=>t.trim().toUpperCase());for(let n=this;n;n=n.parentElement)if(tags.includes(n.tagName))return n;return null;};
    proto.querySelectorAll=function(selector){const nodes=walk(this),attr=selector.match(/^\[([^=\]]+)(?:="([^"]+)")?\]$/);return nodes.filter(n=>selector==='*'||(attr?(attr[2]===undefined?n.getAttribute(attr[1])!=null:n.getAttribute(attr[1])===attr[2]):selector.startsWith('.')?(n.getAttribute('class')??'').split(' ').includes(selector.slice(1)):n.tagName===selector.toUpperCase()));};
  }
  class MutationObserver {
    constructor(callback){this.callback=callback;}
    observe(){document.__copyObservers.push(this.callback);}
    disconnect(){document.__copyObservers=document.__copyObservers.filter(c=>c!==this.callback);}
  }
  return {MutationObserver,queueMicrotask};
}
module.exports={install};
