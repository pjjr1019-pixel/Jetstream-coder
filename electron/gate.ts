export type GateEvidence = {
 paused:boolean; snapshot:string; baseSnapshot:string; baseIntact:boolean;
 check?:{id:number;hash:string;passed:boolean;commandsMatch:boolean};
 review?:{id:number;hash:string;baseHash:string;authorModel:string;reviewerModel:string;approved:boolean};
 approval?:{snapshot:string;baseHash:string;checkId:number;reviewId:number};
};
/** All evidence must describe the same immutable proposal. Unknown is always blocked. */
export function gateReasons(e:GateEvidence):string[] {
 const reasons:string[]=[];
 if(e.paused) reasons.push('Team is paused');
 if(!e.snapshot) reasons.push('Scratch snapshot is unavailable');
 if(!e.baseSnapshot||!e.baseIntact) reasons.push('Original baseline is missing or has changed');
 if(!e.check||!e.check.passed||!e.check.commandsMatch||e.check.hash!==e.snapshot)reasons.push('Current snapshot has no matching successful build and test run');
 if(!e.review||!e.review.approved||!e.review.authorModel.trim()||!e.review.reviewerModel.trim()||e.review.authorModel.trim().toLowerCase()===e.review.reviewerModel.trim().toLowerCase()||e.review.hash!==e.snapshot||e.review.baseHash!==e.baseSnapshot)reasons.push('Current snapshot has no approved review from a different model');
 if(!e.approval||!e.check||!e.review||e.approval.snapshot!==e.snapshot||e.approval.baseHash!==e.baseSnapshot||e.approval.checkId!==e.check.id||e.approval.reviewId!==e.review.id)reasons.push('Phil has not approved this exact snapshot, check run, and review');
 return reasons;
}
