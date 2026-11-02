export function validateManagerResponse({direction,date,quantity,comment}){
 const errors={};
 const needsTarget=direction!=='accept_split';
 if((needsTarget||date)&&!String(quantity).trim())errors.quantity='请填写所需数量；数量和最晚到货日期需要同时填写。';
 else if(String(quantity).trim()&&(!Number.isInteger(Number(quantity))||Number(quantity)<1||Number(quantity)>200))errors.quantity='请输入1至200之间的整数数量（米）。';
 if((needsTarget||String(quantity).trim())&&!date)errors.date='请填写最晚到货日期。';
 if(!comment.trim())errors.comment='请填写现场判断和处理要求。';
 return errors;
}
