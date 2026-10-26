import Foundation
import Vision
import ImageIO
var lastRescue=Date.distantPast
var lastHandAt=Date.distantPast
var lastHands=[[String:Any]]()
var handSequence=0
var lastFaceAt=Date.distantPast
var lastFaces=[[String:Any]]()

// Local video only. Coordinates in output use top-left image origin.
while let line = readLine() {
    autoreleasepool {
        do {
            guard let input = line.data(using: .utf8),
                  let obj = try JSONSerialization.jsonObject(with: input) as? [String: Any],
                  let encoded = obj["jpeg"] as? String, let data = Data(base64Encoded: encoded),
                  data.count < 4_000_000,
                  let source = CGImageSourceCreateWithData(data as CFData, nil),
                  let full = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
                throw NSError(domain: "input", code: 1)
            }
            let started = Date()
            var boxes = [[String: Any]](), poses = [[String: Any]](), hands = [[String: Any]]()
            let handJoints: [(String, VNHumanHandPoseObservation.JointName)] = [
                ("wrist",.wrist), ("thumbCMC",.thumbCMC), ("thumbMP",.thumbMP), ("thumbIP",.thumbIP), ("thumbTip",.thumbTip),
                ("indexMCP",.indexMCP), ("indexPIP",.indexPIP), ("indexDIP",.indexDIP), ("indexTip",.indexTip),
                ("middleMCP",.middleMCP), ("middlePIP",.middlePIP), ("middleDIP",.middleDIP), ("middleTip",.middleTip),
                ("ringMCP",.ringMCP), ("ringPIP",.ringPIP), ("ringDIP",.ringDIP), ("ringTip",.ringTip),
                ("littleMCP",.littleMCP), ("littlePIP",.littlePIP), ("littleDIP",.littleDIP), ("littleTip",.littleTip)
            ]
            func handInfer(_ image: CGImage, _ region: CGRect, _ maximum: Int) throws {
                let request = VNDetectHumanHandPoseRequest(); request.maximumHandCount = maximum
                try VNImageRequestHandler(cgImage:image,orientation:.up).perform([request])
                for observation in request.results ?? [] {
                    var points = [String:[String:Double]]()
                    for (label,joint) in handJoints {
                        let p = try observation.recognizedPoint(joint)
                        if p.confidence >= 0.25 {
                            points[label] = ["x":region.minX+p.location.x*region.width,
                                "y":region.minY+(1-p.location.y)*region.height,"confidence":Double(p.confidence)]
                        }
                    }
                    guard points.count >= 6, let wrist=points["wrist"] else {continue}
                    if hands.contains(where: { h in
                        guard let j=h["joints"] as? [String:[String:Double]],let w=j["wrist"] else{return false}
                        return hypot(w["x"]!-wrist["x"]!,w["y"]!-wrist["y"]!) < 0.035
                    }) {continue}
                    let confidence=points.values.map{$0["confidence"]!}.reduce(0,+)/Double(points.count)
                    hands.append(["joints":points,"confidence":confidence,
                        "side":observation.chirality == .left ? "left" : observation.chirality == .right ? "right" : "unknown"])
                }
            }
            let joints: [(String, VNHumanBodyPoseObservation.JointName)] = [
                ("nose", .nose), ("neck", .neck), ("root", .root),
                ("leftShoulder", .leftShoulder), ("rightShoulder", .rightShoulder),
                ("leftElbow", .leftElbow), ("rightElbow", .rightElbow),
                ("leftWrist", .leftWrist), ("rightWrist", .rightWrist),
                ("leftHip", .leftHip), ("rightHip", .rightHip),
                ("leftKnee", .leftKnee), ("rightKnee", .rightKnee),
                ("leftAnkle", .leftAnkle), ("rightAnkle", .rightAnkle)
            ]
            func infer(_ image: CGImage, _ region: CGRect) throws {
                let people = VNDetectHumanRectanglesRequest()
                people.upperBodyOnly = false
                let pose = VNDetectHumanBodyPoseRequest()
                try VNImageRequestHandler(cgImage: image, orientation: .up).perform([people, pose])
                for person in people.results ?? [] where person.confidence >= 0.55 {
                    let b = person.boundingBox
                    boxes.append(["x":Double(region.minX+b.minX*region.width),
                                  "y":Double(region.minY+(1-b.maxY)*region.height),
                                  "w":Double(b.width*region.width),"h":Double(b.height*region.height),
                                  "confidence":Double(person.confidence)])
                }
                for observation in pose.results ?? [] {
                    var points = [String: [String: Double]]()
                    for (label, joint) in joints {
                        let p = try observation.recognizedPoint(joint)
                        if p.confidence >= 0.4 {
                            points[label] = ["x":region.minX+p.location.x*region.width,
                                             "y":region.minY+(1-p.location.y)*region.height,
                                             "confidence":Double(p.confidence)]
                        }
                    }
                    poses.append(["confidence":Double(observation.confidence),"joints":points])
                    // Body pose itself is a person detector. Retain a well-supported
                    // visible torso when tables hide the full-body rectangle.
                    if points.count >= 6, let left = points["leftShoulder"], let right = points["rightShoulder"],
                       left["confidence"]! >= 0.5, right["confidence"]! >= 0.5,
                       (points["root"] != nil || points["neck"] != nil) {
                        let xs=points.values.map{$0["x"]!}, ys=points.values.map{$0["y"]!}
                        let x=max(0,xs.min()!-0.02),y=max(0,ys.min()!-0.02)
                        let width=min(1,xs.max()!+0.02)-x,height=min(1,ys.max()!+0.02)-y
                        let confidence=points.values.map{$0["confidence"]!}.reduce(0,+)/Double(points.count)
                        if confidence >= 0.65 {
                            boxes.append(["x":x,"y":y,"w":width,"h":height,"confidence":confidence,"source":"body_pose"])
                        }
                    }
                }
            }
            try infer(full,CGRect(x:0,y:0,width:1,height:1))
            let supplied=obj["person_rois"] as? [[String:Double]] ?? []
            if poses.allSatisfy({ (($0["joints"] as? [String:Any])?.count ?? 0) < 6 }) && (!supplied.isEmpty || Date().timeIntervalSince(lastRescue)>0.5) {
                lastRescue=Date()
                let regions=supplied.isEmpty ? [CGRect(x:0,y:0,width:0.6,height:1),CGRect(x:0.4,y:0,width:0.6,height:1)] : supplied.prefix(2).map { b in
                    CGRect(x:(b["x"] ?? 0)-0.04,y:(b["y"] ?? 0)-0.05,width:(b["w"] ?? 0)+0.08,height:(b["h"] ?? 0)+0.10).intersection(CGRect(x:0,y:0,width:1,height:1))
                }
                for r in regions where r.width>0 && r.height>0 {
                    let px=CGRect(x:r.minX*Double(full.width),y:r.minY*Double(full.height),width:r.width*Double(full.width),height:r.height*Double(full.height)).integral
                    if let crop=full.cropping(to:px) {try infer(crop,CGRect(x:px.minX/Double(full.width),y:px.minY/Double(full.height),width:px.width/Double(full.width),height:px.height/Double(full.height)))}
                }
            }
            let bodyMilliseconds=Date().timeIntervalSince(started)*1000
            // Wrist crops preserve the source pixels of small hands in a wide room view.
            // Limit work; no inferred or low-confidence joints are fabricated.
            let handInterval=min(0.25,max(0,(obj["hand_interval_ms"] as? Double ?? 0)/1000))
            let sampleHands=Date().timeIntervalSince(lastHandAt)>=handInterval
            if sampleHands {
            var regions=[CGRect]()
            for person in poses.prefix(3) {
                guard let pts=person["joints"] as? [String:[String:Double]] else {continue}
                for side in ["leftWrist","rightWrist"] {
                    if let q=pts[side] {
                        let r=CGRect(x:max(0,q["x"]!-0.075),y:max(0,q["y"]!-0.11),width:0.15,height:0.22).intersection(CGRect(x:0,y:0,width:1,height:1))
                        if !regions.contains(where:{$0.intersection(r).width*$0.intersection(r).height > r.width*r.height*0.65}) {regions.append(r)}
                    }
                }
            }
            if regions.isEmpty {try handInfer(full,CGRect(x:0,y:0,width:1,height:1),4)}
            else {
                for r in regions.prefix(4) {
                    let px=CGRect(x:r.minX*Double(full.width),y:r.minY*Double(full.height),width:r.width*Double(full.width),height:r.height*Double(full.height)).integral
                    if let crop=full.cropping(to:px) {try handInfer(crop,CGRect(x:px.minX/Double(full.width),y:px.minY/Double(full.height),width:px.width/Double(full.width),height:px.height/Double(full.height)),1)}
                }
            }
            handSequence += 1
            for i in hands.indices { hands[i]["sample_id"]=handSequence }
            lastHands=hands;lastHandAt=Date()
            } else if Date().timeIntervalSince(lastHandAt)<0.28 { hands=lastHands }
            let handMilliseconds=Date().timeIntervalSince(started)*1000-bodyMilliseconds
            // Remove duplicate full-body and pose-supported boxes.
            boxes.sort { ($0["w"] as! Double)*($0["h"] as! Double) > ($1["w"] as! Double)*($1["h"] as! Double) }
            var unique = [[String:Any]]()
            for b in boxes {
                let x=b["x"] as! Double,y=b["y"] as! Double,w=b["w"] as! Double,h=b["h"] as! Double
                let duplicate=unique.contains { a in
                    let ax=a["x"] as! Double,ay=a["y"] as! Double,aw=a["w"] as! Double,ah=a["h"] as! Double
                    let intersection=max(0,min(x+w,ax+aw)-max(x,ax))*max(0,min(y+h,ay+ah)-max(y,ay))
                    return intersection/max(0.000001,min(w*h,aw*ah)) > 0.6
                }
                if !duplicate {unique.append(b)}
            }
            // Low-rate face orientation is an attention cue, not identity or
            // proof of precise eye gaze. Never report a distant/tiny face as sure.
            let faceNow=Date()
            if obj["attention_enabled"] as? Bool == true && faceNow.timeIntervalSince(lastFaceAt)>0.18,
               let eyeEncoded=obj["face_jpeg"] as? String,let eyeData=Data(base64Encoded:eyeEncoded),eyeData.count<4_000_000,
               let eyeSource=CGImageSourceCreateWithData(eyeData as CFData,nil),let eyeImage=CGImageSourceCreateImageAtIndex(eyeSource,0,nil) {
                lastFaceAt=faceNow;lastFaces=[]
                let detector=VNDetectFaceRectanglesRequest()
                detector.revision=VNDetectFaceRectanglesRequestRevision3
                let handler=VNImageRequestHandler(cgImage:eyeImage,orientation:.up)
                try handler.perform([detector])
                let request=VNDetectFaceLandmarksRequest()
                request.inputFaceObservations=detector.results
                try handler.perform([request])
                for f in request.results ?? [] {
                    let b=f.boundingBox, pixels=b.height*Double(eyeImage.height)
                    let original=(detector.results ?? []).min { a,b in
                        hypot(a.boundingBox.midX-f.boundingBox.midX,a.boundingBox.midY-f.boundingBox.midY) < hypot(b.boundingBox.midX-f.boundingBox.midX,b.boundingBox.midY-f.boundingBox.midY)
                    }
                    let yaw=original?.yaw?.doubleValue,pitch=original?.pitch?.doubleValue
                    let eyes=(f.landmarks?.leftEye?.pointCount ?? 0)>=4 && (f.landmarks?.rightEye?.pointCount ?? 0)>=4
                    func eyeEvidence(_ contour:VNFaceLandmarkRegion2D?,_ pupil:VNFaceLandmarkRegion2D?,_ name:String)->[String:Any] {
                        guard let contour=contour,contour.pointCount>=4 else{return ["side":name,"usable":false]}
                        let pts=(0..<contour.pointCount).map { i in CGPoint(x:Double(contour.normalizedPoints[i].x)*b.width*Double(eyeImage.width),y:Double(contour.normalizedPoints[i].y)*b.height*Double(eyeImage.height)) }
                        let minX=pts.map{$0.x}.min()!,maxX=pts.map{$0.x}.max()!,minY=pts.map{$0.y}.min()!,maxY=pts.map{$0.y}.max()!
                        let width=maxX-minX,height=maxY-minY
                        var e:[String:Any]=["side":name,"width_px":width,"height_px":height,"usable":false]
                        guard let pupil=pupil,pupil.pointCount>0,width>0,height>0 else{return e}
                        let p=pupil.normalizedPoints[0],px=Double(p.x)*b.width*Double(eyeImage.width),py=Double(p.y)*b.height*Double(eyeImage.height)
                        let u=(px-minX)/width-0.5,v=(py-minY)/height-0.5
                        e["offset_x"]=u;e["offset_y"]=v;e["pupil_present"]=true
                        e["usable"]=width>=12 && height>=3 && height/width>=0.14 && height/width<=0.8
                        return e
                    }
                    let eyeRows=[eyeEvidence(f.landmarks?.leftEye,f.landmarks?.leftPupil,"left"),eyeEvidence(f.landmarks?.rightEye,f.landmarks?.rightPupil,"right")]
                    let eyeClear=eyeRows.allSatisfy{$0["usable"] as? Bool == true}
                    let centered=eyeClear && eyeRows.allSatisfy{abs($0["offset_x"] as? Double ?? 9)<0.18 && abs($0["offset_y"] as? Double ?? 9)<0.32}
                    let consistent=abs((eyeRows[0]["offset_x"] as? Double ?? 9)-(eyeRows[1]["offset_x"] as? Double ?? -9))<0.16
                    let headFacing=pixels>=35 && eyes && f.confidence>=0.65 && abs(yaw ?? 9)<0.35 && abs(pitch ?? 9)<0.55
                    var row:[String:Any]=["box":["x":b.minX,"y":1-b.maxY,"w":b.width,"h":b.height],"confidence":Double(f.confidence),"face_pixels":pixels,
                        "facing_candidate":headFacing,"eyes":eyeRows,"eyes_clear":eyeClear,"gaze_candidate":headFacing && centered && consistent,"gaze_method":"head_and_pupil_geometry_experimental","gaze_source_size":[eyeImage.width,eyeImage.height]]
                    if let yaw=yaw {row["yaw"]=yaw};if let pitch=pitch {row["pitch"]=pitch}
                    lastFaces.append(row)
                }
            }
            let faceAge=Date().timeIntervalSince(lastFaceAt)*1000
            let faces=faceAge<400 ? lastFaces.map { f -> [String:Any] in var row=f;row["sample_age_ms"]=faceAge;return row } : []
            let result: [String: Any] = ["boxes":unique,"poses":poses,"hands":hands,"faces":faces,
                "body_ms":bodyMilliseconds,"hand_ms":handMilliseconds,"hands_sampled":sampleHands,
                "inference_ms":Date().timeIntervalSince(started)*1000]
            let out = try JSONSerialization.data(withJSONObject: result,options:[.sortedKeys])
            print(String(decoding:out,as:UTF8.self));fflush(stdout)
        } catch {
            let ns=error as NSError
            let result: [String: Any] = ["error":"vision_request_failed","domain":ns.domain,"code":ns.code,"boxes":[],"poses":[]]
            let out=(try? JSONSerialization.data(withJSONObject:result)) ?? Data()
            print(String(decoding:out,as:UTF8.self));fflush(stdout)
        }
    }
}
