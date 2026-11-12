export const soundReviewNames={crying:'疑似哭声',scream:'疑似尖叫声',glass_break:'疑似破碎声',alarm:'疑似警报声'};
export const isSafetyOutputTest=event=>event.simulated&&event.status==='test';
export function safetyEventTitle(event,{includePerson=true}={}){
 if(isSafetyOutputTest(event))return '播报测试';
 if(event.kind==='visual_review')return '画面待复核';
 if(event.kind==='sound_review')return soundReviewNames[event.sound_kind]||'声音待核对';
 if(event.kind==='fall')return includePerson?(event.person_name?event.person_name+' · 疑似跌倒':'有人疑似跌倒'):'疑似跌倒';
 return '待核对提醒';
}
