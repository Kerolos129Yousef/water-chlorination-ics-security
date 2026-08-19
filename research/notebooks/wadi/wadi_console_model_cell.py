# ============================================================
#  Export the WADI DenseAE as a browser model for Console Pro
#  Run in SWaT_Anomaly_Detection AFTER the WADI models exist
#  (wadi_models, wadi_scaler, wadi_meta from the WADI run).
#  Produces model_web_wadi.json -> in the console, pick WADI and "load model JSON".
# ============================================================
import json, numpy as np
ae = wadi_models["DenseAE"].to("cpu").eval()
feats = wadi_meta["features"]

# threshold = 99th percentile of per-row recon error on WADI NORMAL data
X, y = load_dataset(CONFIG["DATASETS"]["WADI"], {**CONFIG, **CONFIG["DATASET_OVERRIDES"].get("WADI",{}), "_dataset_name":"WADI"})
Xtr, Xval, Xte, yte, _ = make_splits(X, y, {**CONFIG, **CONFIG["DATASET_OVERRIDES"].get("WADI",{}), "_dataset_name":"WADI"})
tr_err = recon_error(ae, Xtr, seq=False)
thr = float(np.percentile(tr_err, 99))

sd = {k.replace("enc.","encoder.").replace("dec.","decoder."): np.asarray(v,dtype=float) for k,v in ae.state_dict().items()}
rnd = lambda a: [round(float(x),6) for x in np.asarray(a).ravel()]
model = {"features": feats, "mean": rnd(wadi_scaler.mean_), "scale": rnd(wadi_scaler.scale_),
         "threshold": round(thr,8),
         "layers": [{"W":[rnd(r) for r in sd[f"encoder.{i}.weight"]],"b":rnd(sd[f"encoder.{i}.bias"]),"act":"relu"} for i in (0,2,4)]
                  + [{"W":[rnd(r) for r in sd[f"decoder.{i}.weight"]],"b":rnd(sd[f"decoder.{i}.bias"]),"act":("relu" if i<4 else "none")} for i in (0,2,4)]}
json.dump(model, open("model_web_wadi.json","w"))
print("wrote model_web_wadi.json |", len(feats), "features | threshold", round(thr,5))
