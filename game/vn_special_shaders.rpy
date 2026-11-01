# GLES3 ports of Sprite-ScriptActionFigure{Erasure,ErasureReverse,Wipe,Flash} and
# BackFlashSheder recovered from the supplied APK. No procedural replacement masks.
init 10 python:
    renpy.register_shader("fgo.figure_special", variables="""
        uniform float u_lod_bias;
        uniform sampler2D tex0;
        uniform sampler2D tex1;
        uniform float u_vns_volume;
        uniform float u_vns_mode;
        uniform float u_vns_split;
        uniform vec4 u_vns_filter;
        uniform vec4 u_vns_uv;
        attribute vec2 a_tex_coord;
        varying vec2 v_tex_coord;
    """, vertex_200="""
        v_tex_coord = a_tex_coord;
    """, fragment_200="""
        vec4 pm = texture2D(tex0, v_tex_coord, u_lod_bias);
        vec4 c = vec4(pm.a > 0.0 ? pm.rgb / pm.a : vec3(0.0), pm.a);
        // Unity Mesh.uv2 is in the BODY texture's space, including for the face quad.
        vec2 uv = u_vns_uv.xy + v_tex_coord * u_vns_uv.zw;
        float threshold = uv.y;
        if (u_vns_mode > 0.5 && u_vns_mode < 1.5) threshold = 1.0-uv.y;
        if (u_vns_mode > 1.5) threshold = texture2D(tex1, vec2(uv.x,1.0-uv.y), u_lod_bias).r;
        float d = u_vns_volume-threshold;
        float t = d/0.2;
        bool colored = u_vns_filter.a > 0.0;
        vec4 result = c;
        if (u_vns_mode > 3.5) {
            // Native soft BackFlashSheder: complementary alpha, RGB = _Color.
            float a = d > 0.2 ? 0.0 : (d > 0.0 ? 1.0-t : 1.0);
            result = vec4(u_vns_filter.rgb,a);
        } else if (d <= 0.0) {
            result = vec4(colored ? u_vns_filter.rgb : c.rgb, u_vns_mode > 2.5 ? c.a : 0.0);
        } else if (d <= 0.2) {
            vec3 rgb = colored ? mix(u_vns_filter.rgb,c.rgb,t) : c.rgb;
            float a = t*c.a;
            // RGBA Erasure/Combine interpolate filter alpha too; split-alpha variants
            // only interpolate RGB. Wipe always scales source alpha; Flash preserves it.
            if (u_vns_mode < 1.5 && u_vns_split < 0.5 && colored)
                a = t*mix(u_vns_filter.a,c.a,t);
            if (u_vns_mode > 2.5) a = c.a;
            result = vec4(rgb,a);
        }
        // FGO's straight-alpha shader result -> Ren'Py premultiplied representation.
        gl_FragColor = vec4(result.rgb*result.a,result.a);
    """)

    VN_SPECIAL_MASKS = {"circleIn": "assets/special_fx/circleIn.png",
                        "circleOut": "assets/special_fx/circleOut.png"}

    def vn_special_uv(data):
        # cctor: Body 1022x766. Body-normalized TEXCOORD1 corner samples
        # x=1.5..1022.5 and y=1022.5..257.5. The existing reader raster is 1024x768.
        return (1.5/1024.0,1022.5/1024.0,1021.0/1024.0,-765.0/1024.0)

    def vn_special_mesh(child, mask, rec, size, back=False, split=True):
        bottom = renpy.render(child,size[0],size[1],0,0)
        if not renpy.loadable(VN_SPECIAL_MASKS[mask]):
            return bottom  # mask art not shipped: draw the figure unmasked
        top = renpy.render(Transform(VN_SPECIAL_MASKS[mask],xysize=size),size[0],size[1],0,0)
        rv = renpy.Render(*size)
        rv.mesh = True
        rv.blit(bottom,(0,0),focus=True,main=True)
        rv.blit(top,(0,0),focus=False,main=False)
        rv.add_shader("fgo.figure_special")
        rv.add_uniform("u_vns_volume",vn_special_volume(rec))
        rv.add_uniform("u_vns_mode",4.0 if back else float(rec["mode"]))
        rv.add_uniform("u_vns_filter",rec["color"])
        rv.add_uniform("u_vns_split",float(bool(split)))
        rv.add_uniform("u_vns_uv",(0.0,1.0,1.0,-1.0) if back else vn_special_uv(None))
        return rv

    class VNFigureSpecial(renpy.Displayable):
        def __init__(self,slot,child):
            super(VNFigureSpecial,self).__init__()
            self.slot,self.child = slot,renpy.displayable(child)

        def render(self,width,height,st,at):
            data = store.current_chara_defs.get(self.slot) or {}
            rec = vn_special_active(data)
            if rec is None:
                return renpy.render(self.child,width,height,st,at)
            rv = vn_special_mesh(self.child,rec["mask"],rec,(1024,768),
                                 split=data.get("native_alpha_split",True))
            if not rec["paused"]:
                renpy.redraw(self,0)
            return rv

        def visit(self):
            return [self.child]

    class VNFlashQuad(renpy.Displayable):
        def __init__(self,rec):
            super(VNFlashQuad,self).__init__()
            self.rec = rec

        def render(self,width,height,st,at):
            rv = vn_special_mesh(Solid("#ffffff",xysize=(2049,1153)),
                                 self.rec["mask"],self.rec,(2049,1153),back=True)
            if not self.rec["paused"]:
                renpy.redraw(self,0)
            return rv

    class VNFlashBack(renpy.Displayable):
        def __init__(self,slot,owner,rec):
            super(VNFlashBack,self).__init__()
            self.slot,self.owner,self.rec = slot,owner,rec
            self.quad = VNFlashQuad(rec)
            # SpecialEffectBase.xy is GetCenterOffset (0,-288), z=0; back mesh z=+2.
            self.rig = dict(slot=slot,owner=owner,offset=rec["offset"],flip="")

        def render(self,width,height,st,at):
            rec = self.rec
            if store.current_chara_defs.get(self.slot) is not self.owner or not vn_special_alive(rec):
                return renpy.Render(width,height)
            # Serialized BackWipeMesh mWidth/mHeight, pivot center, isRescale=false.
            x,y,scale,m = vn_fx_geometry(self.rig)
            matrix = Matrix([m[0][0],-m[0][1],0.,0.,-m[1][0],m[1][1],0.,0.,
                             0.,0.,1.,0.,0.,0.,0.,1.])
            placed = Transform(self.quad,anchor=(.5,.5),xpos=absolute(width*.5+x),ypos=absolute(height*.5-y),
                               zoom=scale,matrixanchor=(.5,.5),matrixtransform=matrix)
            rv = renpy.render(Fixed(placed,xysize=(width,height)),width,height,st,at)
            if not rec["paused"]:
                renpy.redraw(self,0)
            return rv

        def visit(self):
            return [self.quad]

    def vn_special_back_display(slot,data,rec):
        return VNFlashBack(slot,data,rec)
