(function () {
    "use strict";
    var $$find = function (arr, predicate) {
        if (typeof predicate !== "function") {
            throw new TypeError("predicate must be a function");
        }
        var length = arr.length >>> 0;
        var thisArg = arguments[1];
        var value;
        for (var i = 0; i < length; i++) {
            value = arr[i];
            if (predicate.call(thisArg, value, i, arr)) {
                return value;
            }
        }
        return undefined;
    };
    var LabelElement = (function () {
        function LabelElement(_a, params) {
            var node = _a.node, _b = _a.position, position = _b === void 0 ? null : _b, _c = _a.data, data = _c === void 0 ? null : _c;
            this.updateParams(params);
            this._node = node;
            this.initStyles(params.cssClass);
            if (data) {
                this.updateData(data);
            }
            if (position) {
                this.updatePosition(position);
            }
        }
        LabelElement.prototype.updateParams = function (_a) {
            var _b = _a.tpl, tpl = _b === void 0 ? function () { return ""; } : _b, _c = _a.cssClass, cssClass = _c === void 0 ? null : _c, _d = _a.halign, halign = _d === void 0 ? "center" : _d, _e = _a.valign, valign = _e === void 0 ? "center" : _e, _f = _a.ealign, ealign = _f === void 0 ? "midpoint" : _f, _g = _a.halignBox, halignBox = _g === void 0 ? "center" : _g, _h = _a.valignBox, valignBox = _h === void 0 ? "center" : _h;
            var _align = {
                "top": -.5,
                "left": -.5,
                "center": 0,
                "right": .5,
                "bottom": .5
            };
            this._align = [
                _align[halign],
                _align[valign],
                100 * (_align[halignBox] - 0.5),
                100 * (_align[valignBox] - 0.5)
            ];
            this.tpl = tpl;
        };
        LabelElement.prototype.updateData = function (data) {
            try {
                this._node.innerHTML = this.tpl(data);
            }
            catch (err) {
                console.error(err);
            }
        };
        LabelElement.prototype.getNode = function () {
            return this._node;
        };
        LabelElement.prototype.updatePosition = function (pos) {
            this._renderPosition(pos);
        };
        LabelElement.prototype.initStyles = function (cssClass) {
            var stl = this._node.style;
            stl.position = 'absolute';
            if (cssClass && cssClass.length) {
                this._node.classList.add(cssClass);
            }
        };
        LabelElement.prototype._renderPosition = function (position) {
            var prev = this._position;
            var x = position.x + this._align[0] * position.w;
            var y = position.y + this._align[1] * position.h;
            var a = position.a;
            if (!prev || prev[0] !== x || prev[1] !== y) {
                this._position = [x, y];
                var valRel = "translate(" + this._align[2] + "%," + this._align[3] + "%) ";
                var valAbs = "translate(" + x.toFixed(2) + "px," + y.toFixed(2) + "px) ";
                var val = valRel + valAbs;
                var stl = this._node.style;
                if (a) {
                    val += "rotate(" + a.toFixed(2) + "deg)";
                    var xo = Math.abs(this._align[2]);
                    var yo = Math.abs(this._align[3]);
                    stl.transformOrigin = xo + "% " + yo + "% 0px";
                }
                stl.webkitTransform = val;
                stl.msTransform = val;
                stl.transform = val;
            }
        };
        return LabelElement;
    }());
    var LabelContainer = (function () {
        function LabelContainer(node) {
            this._node = node;
            this._elements = {};
        }
        LabelContainer.prototype.addOrUpdateElem = function (id, param, payload) {
            if (payload === void 0) { payload = {}; }
            var cur = this._elements[id];
            if (cur) {
                cur.updateParams(param);
                cur.updateData(payload.data);
                cur.updatePosition(payload.position);
            }
            else {
                var nodeElem = document.createElement("div");
                this._node.appendChild(nodeElem);
                this._elements[id] = new LabelElement({
                    node: nodeElem,
                    data: payload.data,
                    position: payload.position
                }, param);
            }
        };
        LabelContainer.prototype.removeElemById = function (id) {
            if (this._elements[id]) {
                this._node.removeChild(this._elements[id].getNode());
                delete this._elements[id];
            }
        };
        LabelContainer.prototype.updateElemPosition = function (id, position) {
            var ele = this._elements[id];
            if (ele) {
                ele.updatePosition(position);
            }
        };
        LabelContainer.prototype.updatePanZoom = function (_a) {
            var pan = _a.pan, zoom = _a.zoom;
            var val = "translate(" + pan.x + "px," + pan.y + "px) scale(" + zoom + ")";
            var stl = this._node.style;
            var origin = "top left";
            stl.webkitTransform = val;
            stl.msTransform = val;
            stl.transform = val;
            stl.webkitTransformOrigin = origin;
            stl.msTransformOrigin = origin;
            stl.transformOrigin = origin;
        };
        LabelContainer.prototype.has = function (id) {
            return this._elements[id] !== undefined;
        };
        return LabelContainer;
    }());
    function cyHtmlLabel(_cy, params) {
        var _params = (!params || typeof params !== "object") ? [] : params;
        var _lc = createLabelContainer();
        _cy.one("render", function (e) {
            createCyHandler(e);
            wrapCyHandler(e);
        });
        _cy.on("add", addCyHandler);
        _cy.on("layoutstop", layoutstopHandler);
        _cy.on("remove", removeCyHandler);
        _cy.on("data", updateDataCyHandler);
        _cy.on("pan zoom", wrapCyHandler);
        _cy.on("position bounds", moveCyHandler);
        return _cy;
        function createLabelContainer() {
            var _cyContainer = _cy.container();
            var _titlesContainer = document.createElement("div");
            var _cyCanvas = _cyContainer.querySelector("canvas");
            var cur = _cyContainer.querySelector("[class^='cy-html']");
            if (cur) {
                _cyCanvas.parentNode.removeChild(cur);
            }
            var stl = _titlesContainer.style;
            stl.position = 'absolute';
            stl['z-index'] = 10;
            stl.width = '500px';
            stl['pointer-events'] = 'none';
            stl.margin = '0px';
            stl.padding = '0px';
            stl.border = '0px';
            stl.outline = '0px';
            stl.outline = '0px';
            _cyCanvas.parentNode.appendChild(_titlesContainer);
            return new LabelContainer(_titlesContainer);
        }
        function createCyHandler(_a) {
            var cy = _a.cy;
            _params.forEach(function (x) {
                cy.elements(x.query).forEach(function (d) {
                    _lc.addOrUpdateElem(d.id(), x, {
                        position: getPosition(d),
                        data: d.data()
                    });
                });
            });
        }
        function addCyHandler(ev) {
            var target = ev.target;
            var param = $$find(_params.slice().reverse(), function (x) { return target.is(x.query); });
            if (param) {
                _lc.addOrUpdateElem(target.id(), param, {
                    position: getPosition(target),
                    data: target.data()
                });
            }
        }
        function layoutstopHandler(_a) {
            var cy = _a.cy;
            _params.forEach(function (x) {
                cy.elements(x.query).forEach(function (d) {
                    _lc.updateElemPosition(d.id(), getPosition(d));
                });
            });
        }
        function removeCyHandler(ev) {
            _lc.removeElemById(ev.target.id());
        }
        function moveCyHandler(ev) {
            if (_lc.has(ev.target.id()) ||
                ev.target.connectedEdges(function (ele) { return _lc.has(ele.id()); }).size()) {
                _lc.updateElemPosition(ev.target.id(), getPosition(ev.target));
                ev.target.connectedEdges().forEach(function (el) {
                    _lc.updateElemPosition(el.id(), getPosition(el));
                });
            }
        }
        function updateDataCyHandler(ev) {
            setTimeout(function () {
                var target = ev.target;
                var param = $$find(_params.slice().reverse(), function (x) { return target.is(x.query); });
                if (param) {
                    _lc.addOrUpdateElem(target.id(), param, {
                        position: getPosition(target),
                        data: target.data()
                    });
                }
                else {
                    _lc.removeElemById(target.id());
                }
            }, 0);
        }
        function wrapCyHandler(_a) {
            var cy = _a.cy;
            _lc.updatePanZoom({
                pan: cy.pan(),
                zoom: cy.zoom()
            });
        }
        function lineAngle(p0, p1) {
            var dx = p1.x - p0.x;
            var dy = p1.y - p0.y;
            var angle = Math.atan(dy / dx);
            if (dx === 0 && angle < 0) {
                angle = angle * -1;
            }
            return angle * 180 / Math.PI;
        }
        function getPosition(el) {
            if (el.isNode()) {
                return {
                    w: el.width(),
                    h: el.height(),
                    x: el.position("x"),
                    y: el.position("y"),
                    a: 0
                };
            }
            else if (el.isEdge()) {
                var param = $$find(_params.slice().reverse(), function (x) { return el.is(x.query); });
                if (param) {
                    var pos = void 0, angle = 0;
                    if (param.ealign === 'source') {
                        pos = el.sourceEndpoint();
                    }
                    else if (param.ealign === 'target') {
                        pos = el.targetEndpoint();
                    }
                    else {
                        pos = el.midpoint();
                    }
                    if (param.autorotate || el.data('label_autorotate')) {
                        angle = lineAngle(el.sourceEndpoint(), el.targetEndpoint());
                    }
                    return {
                        w: 0,
                        h: 0,
                        x: pos.x,
                        y: pos.y,
                        a: angle
                    };
                }
            }
        }
    }
    var register = function (cy) {
        if (!cy) {
            return;
        }
        cy("core", "htmlLabel", function (optArr) {
            return cyHtmlLabel(this, optArr);
        });
    };
    if (typeof module !== "undefined" && module.exports) {
        module.exports = function (cy) {
            register(cy);
        };
    }
    else {
        if (typeof define !== "undefined" && define.amd) {
            define("cytoscape-htmlLabel", function () {
                return register;
            });
        }
    }
    if (typeof cytoscape !== "undefined") {
        register(cytoscape);
    }
}());
//# sourceMappingURL=cytoscape-html-label.js.map