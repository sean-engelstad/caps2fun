#!/usr/bin/env python
"""
This file is part of the package FUNtoFEM for coupled aeroelastic simulation
and design optimization.

Copyright (C) 2015 Georgia Tech Research Corporation.
Additional copyright (C) 2015 Kevin Jacobson, Jan Kiviaho and Graeme Kennedy.
All rights reserved.

FUNtoFEM is licensed under the Apache License, Version 2.0 (the "License");
you may not use this software except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.


script to run funtofem from design variable inputs, funtofem.input file and .csm files
returns function, gradient information in funtofem.out file

Aerothermoelastic Optimization with FuntoFem and ESP/CAPS
	Design Problem: NACA Symmetric Wing
Authors: Sean Engelstad, Sejal Sahu, Graeme Kennedy
	Georgia Tech SMDO Lab April 2022
"""

#--------------------------- File Checklist --------------------------------------#

# Files to make sure you have before running F2F
# 1) fun3d.nml in steady/Flow folder
# 2) pointwise.mapbc if running pointwise mesher
# 3) a fluid CSM file for ESP/CAPS, fluid volume mesh
# 4) a struct CSM file for ESP/CAPS, the struct surf mesh

#check the DVdict was read in correctly
#print(DVdict)

#FUNtoFEM software is used here for full aerothermoelastic optimization of a NACA wing
#   with parametric geometries built by ESP/CAPS
#Authors: Sean Engelstad, Sejal Sahu, Graeme Kennedy
#Work for Aviation Paper 2022 on Full aerothermoelastic optimization with Parametric geometries of ESP/CAPS

from __future__ import print_function

#import funtofem classes and functions
from pyfuntofem.model  import *
from pyfuntofem.driver import *
from pyfuntofem.fun3d_interface import *
from pyfuntofem.tacs_interface import TacsSteadyInterface

#import from tacs
from tacs.pytacs import pyTACS
from tacs import functions
from tacs import TACS, functions, constitutive, elements, pyTACS, problems

#import normal classes
import os, shutil, sys
import numpy as np

#import other modules
from pyoptsparse import SLSQP, Optimization
import pyCAPS



#subclass for tacs steady interface
class wedgeTACS(TacsSteadyInterface):
    def __init__(self, comm, tacs_comm, model, n_tacs_procs, datFile):
        super(wedgeTACS,self).__init__(comm, tacs_comm, model)

        assembler = None
        nodes = None
        self.tacs_proc = False
        if comm.Get_rank() < n_tacs_procs:
            self.tacs_proc = True

            # Instantiate FEASolver
            structOptions = {
                'printtiming':True,
            }

            FEASolver = pyTACS(datFile, options=structOptions, comm=tacs_comm)

            # Material properties
            rho = 2780.0        # density kg/m^3
            E = 73.1e9          # Young's modulus (Pa)
            nu = 0.33           # Poisson's ratio
            kcorr = 5.0/6.0     # shear correction factor
            ys = 324.0e6        # yield stress
            specific_heat = 920.096
            cte = 24.0e-6
            kappa = 230.0

            tInput = 0.001*np.ones(3)

            def elemCallBack(dvNum, compID, compDescript, elemDescripts, globalDVs, **kwargs):
                elemIndex = kwargs['propID'] - 1
                t = tInput[elemIndex]
                prop = constitutive.MaterialProperties(rho=rho, specific_heat=specific_heat,
                                                       E=E, nu=nu, ys=ys, cte=cte, kappa=kappa)
                con = constitutive.IsoShellConstitutive(prop, t=t, tNum=dvNum)

                elemList = []
                transform = None
                for elemDescript in elemDescripts:
                    if elemDescript in ['CQUAD4', 'CQUADR']:
                        elem = elements.Quad4ThermalShell(transform, con)
                    else:
                        print("Uh oh, '%s' not recognized" % (elemDescript))
                    elemList.append(elem)

                # Add scale for thickness dv
                scale = [1.0]
                return elemList, scale

            # Set up elements and TACS assembler
            FEASolver.initialize(elemCallBack)
            assembler = FEASolver.assembler


            #Tim's fix, based on tacs/pyMeshloader.py
            if (n_tacs_procs > 1):
                ownerRange = assembler.getOwnerRange()
                nodes = np.arange(ownerRange[tacs_comm.rank], ownerRange[tacs_comm.rank+1], dtype=int)
            else:
                origNodePositions = FEASolver.getOrigNodes()
                nodes = np.arange(origNodePositions.shape[0]//3, dtype=int)


        self._initialize_variables(assembler, struct_id=nodes, thermal_index=6)
        self.initialize(model.scenarios[0],model.bodies)

    def post_export_f5(self):
        flag = (TACS.OUTPUT_CONNECTIVITY |
                TACS.OUTPUT_NODES |
                TACS.OUTPUT_DISPLACEMENTS |
                TACS.OUTPUT_STRAINS |
                TACS.OUTPUT_STRESSES |
                TACS.OUTPUT_EXTRAS)
        f5 = TACS.ToFH5(self.assembler, TACS.BEAM_OR_SHELL_ELEMENT, flag)
        f5.writeToFile('TACSoutput.f5')

#class for NACA OML optimization, full aerothermoelastic, with ESP/CAPS parametric geometries
class NacaOMLOptimization():
    def __init__(self, comm, structCSM, fluidCSM, meshStyle = "tetgen", analysisType = "aerothermoelastic"):

        self.comm = comm
        
        #mesh style setting - pointwise or tetgen
        self.mesh_style = meshStyle #"pointwise", "tetgen"

        self.n_tacs_procs = 1

        #read input from file
        self.readInput()

        #clear capslock files
        if (self.comm.Get_rank() == 0): self.clearCapsLock()

        #status file

        if (self.comm.Get_rank() == 0):
            statusFile = os.path.join(os.getcwd(), "funtofem", "status.txt")
            self.status =  open(statusFile, "w")
            

        #into status
        self.cwrite("Running Funtofem with ESP/CAPS\n")


        #iteration counter for optimizer
        self.iteration = 1

        self.curDir = os.getcwd()

        #type of analysis
        self.analysis_type = analysisType

        #load pointwise
        #module load pointwise/18.5R1
        #need to run module load pointwise/18.5R1 in terminal for it to work

        #initialize AIMS
        if (self.comm.Get_rank() == 0): self.initializeAIMs(structCSM, fluidCSM)

    def clearCapsLock(self):
        #delete capsLock files for next run
        if (os.path.exists("struct/Scratch/capsLock")): os.system("rm struct/Scratch/capsLock")
        if (os.path.exists("fluid/Scratch/capsLock")): os.system("rm fluid/Scratch/capsLock")

    def cwrite(self, text):
        if (self.comm.Get_rank() == 0):
            #write to the status file
            self.status.write(text)
        
            #immediately update it to be visibile in the file
            self.status.flush()

    def commBarrier(self, location):
        print("proc #{} reached location {}\n".format(self.comm.Get_rank(), location))
        sys.stdout.flush()
        self.comm.Barrier()

    def readInput(self):
        #initialize DVdict as none for all procs
        self.DVdict = None

        if (self.comm.Get_rank() == 0):
            curDir = os.getcwd()
            #print(curDir)
            funtofemFolder = os.path.join(curDir, "funtofem")

            inputFile = os.path.join(funtofemFolder, "funtofem.in")
            inputHandle =  open(inputFile, "r")
            lines = inputHandle.readlines()

            #read in the DVdict from funtofem.in
            self.DVdict = []
            for line in lines:

                #read line if not empty
                if (len(line) > 1):
                    parts = line.split(",")
                    name = parts[0]
                    dvType = parts[1]
                    if (dvType == "shape"):
                        value = float(parts[2])
                        capsGroup = ""
                    elif (dvType == "struct"):
                        capsGroup = parts[2]
                        value = float(parts[3])
                    
                    #store DVdict
                    tempDict = {"name" : name,
                                "type" : dvType,
                                "capsGroup" : capsGroup,
                                "value" : value}
                    self.DVdict.append(tempDict)

            inputHandle.close()

        #MPI broadcast from root proc
        self.DVdict = self.comm.bcast(self.DVdict, root=0)

    def initializeAIMs(self, structCSM, fluidCSM):
        if (self.comm.Get_rank() == 0):
            #initialize all 6 ESP/CAPS AIMs used for the fluid and structural analysis

            #initialize pyCAPS structural problem
            self.capsStruct = pyCAPS.Problem(problemName = "struct",
                        capsFile = structCSM,
                        outLevel = 1)
            #self.cwrite("Initialized caps Struct AIM\n")

            #initialize pyCAPS fluid problem
            self.capsFluid = pyCAPS.Problem(problemName = "fluid",
                        capsFile = fluidCSM,
                        outLevel = 1)
            #self.cwrite("Initialized caps fluid AIM\n")

            #initialize egads Aim
            self.egadsAim = self.capsStruct.analysis.create(aim="egadsTessAIM")
            #self.cwrite("Initialized egads AIM\n")

            #initialize tacs Aim
            self.tacsAim = self.capsStruct.analysis.create(aim = "tacsAIM", name = "tacs")
            #self.cwrite("Initialized tacs AIM\n")


            if (self.mesh_style == "pointwise"):
                #initialize pointwise AIM
                self.pointwiseAim = self.capsFluid.analysis.create(aim = "pointwiseAIM",
                                            name = "pointwise")
                #self.cwrite("Initialized pointwise AIM\n")
            elif (self.mesh_style == "tetgen"):
                #initialize egads fluid aim
                self.egadsFluidAim = self.capsFluid.analysis.create(aim = "egadsTessAIM", name = "egadsTess")
                #self.cwrite("Initialized egadsTessAIM for fluid\n")
                
                #initialize tetgen aim
                self.tetgenAim = self.capsFluid.analysis.create(aim = "tetgenAIM", name = "tetgen")
                #self.cwrite("Initialized tetgen AIM\n")

            #initialize FUN3D AIM from Pointwise mesh
            self.fun3dAim = self.capsFluid.analysis.create(aim = "fun3dAIM",
                                    name = "fun3d")
            #self.cwrite("Initialized fun3d AIM\n")

            #structural mesh settings
            self.structureMeshSettings()
            #self.cwrite("Set Structure mesh settings\n")

            #fluid mesh settings
            self.fluidMeshSettings()
            #self.cwrite("Set Fluid mesh settings\n")

            #set fun3d settings
            self.fun3dSettings()
            #self.cwrite("Set fun3d settings\n")

    def structureMeshSettings(self):
        #names the bdf and dat files as pointwise.ext or tetgen.ext
        self.tacsAim.input.Proj_Name = self.mesh_style
        
        #Egads Aim section, for mesh
        self.egadsAim.input.Edge_Point_Min = 5
        self.egadsAim.input.Edge_Point_Max = 10

        self.egadsAim.input.Mesh_Elements = "Quad"

        self.egadsAim.input.Tess_Params = [0.25,.01,10]

        #increase the precision in the BDF file
        self.tacsAim.input.File_Format = "Large"
        self.tacsAim.input.Mesh_File_Format = "Large"

        # Link the mesh
        self.tacsAim.input["Mesh"].link(self.egadsAim.output["Surface_Mesh"])

        # Set analysis type
        self.tacsAim.input.Analysis_Type = "Static"

        #materials section    
        madeupium    = {"materialType" : "isotropic",
                        "youngModulus" : 72.0E9 ,
                        "poissonRatio": 0.33,
                        "density" : 2.8E3,
                        "tensionAllow" :  20.0e7}

        self.tacsAim.input.Material = {"madeupium": madeupium}

        # Material properties section
        OMLshell = {"propertyType" : "Shell",
                    "membraneThickness" : 0.01,
                    "material"        : "madeupium",
                    "bendingInertiaRatio" : 1.0, # Default
                    "shearMembraneRatio"  : 5.0/6.0} # Default
        ribshell  = {"propertyType" : "Shell",
                    "membraneThickness" : 0.02,
                    "material"        : "madeupium",
                    "bendingInertiaRatio" : 1.0, # Default
                    "shearMembraneRatio"  : 5.0/6.0} # Default

        sparshell = {"propertyType" : "Shell",
                    "membraneThickness" : 0.05,
                    "material"        : "madeupium",
                    "bendingInertiaRatio" : 1.0, # Default
                    "shearMembraneRatio"  : 5.0/6.0} # Default

        self.tacsAim.input.Property = {"rib": ribshell,
        "spar" : sparshell,
        "OML" : OMLshell}

        # constraint section
        constraint1 = {"groupName" : "wingRoot",
                        "dofConstraint" : 123456}

        self.tacsAim.input.Constraint = {"fixRoot": constraint1}

        # don't set any loads

        ##-----------setup DVs and DVRs-----------##

        #list of design variables, with thick1-thickN for thickness DVs
        desvars = ["area","aspect","taper","ctwist","lesweep","dihedral","thick1", "thick2", "thick3"]
        nvar = len(desvars)

        #where the thickDVs have thick1 is for "rib", thick2 for "spar" etc.
        thickness = 0.01

        #make initial DV and DVR dict
        DVdict = {}
        DVRdict = {}

        #add thickDVs and geomDVs to caps
        thickCt = 0
        for DV in self.DVdict:
            dvname = DV["name"]
            if (DV["type"] == "struct"):
                #add thickDV entry into DV_Relations and DV Dicts
                DVRdict[dvname] = self.makeThicknessDVR(dvname)
                DVdict[dvname] = self.makeThicknessDV(DV["capsGroup"],thickness)
                thickCt += 1
            elif (DV["type"] == "shape"): #geomDV, add empty entry into DV dicts
                DVdict[dvname] = {}

        #input DVdict and DVRdict into tacsAim
        self.tacsAim.input.Design_Variable = DVdict
        self.tacsAim.input.Design_Variable_Relation = DVRdict

    def fluidMeshSettings(self):
        if (self.mesh_style == "pointwise"):
            # Dump VTK files for visualization
            self.pointwiseAim.input.Proj_Name   = "TransportWing"
            self.pointwiseAim.input.Mesh_Format = "VTK"

            # Connector level
            self.pointwiseAim.input.Connector_Turn_Angle       = 10
            self.pointwiseAim.input.Connector_Prox_Growth_Rate = 1.2
            self.pointwiseAim.input.Connector_Source_Spacing   = True

            # Domain level
            self.pointwiseAim.input.Domain_Algorithm    = "AdvancingFront"
            self.pointwiseAim.input.Domain_Max_Layers   = 15
            self.pointwiseAim.input.Domain_Growth_Rate  = 1.25
            self.pointwiseAim.input.Domain_TRex_ARLimit = 40.0
            self.pointwiseAim.input.Domain_Decay        = 0.8
            self.pointwiseAim.input.Domain_Iso_Type = "Triangle"

            # Block level
            self.pointwiseAim.input.Block_Boundary_Decay       = 0.8
            self.pointwiseAim.input.Block_Collision_Buffer     = 1.0
            self.pointwiseAim.input.Block_Max_Skew_Angle       = 160.0
            self.pointwiseAim.input.Block_Edge_Max_Growth_Rate = 1.5
            self.pointwiseAim.input.Block_Full_Layers          = 1
            self.pointwiseAim.input.Block_Max_Layers           = 100
            self.pointwiseAim.input.Block_TRexType = "TetPyramid"
            #T-Rex cell type (TetPyramid, TetPyramidPrismHex, AllAndConvertWallDoms).

            # Set wall spacing for capsMesh == leftWing and capsMesh == riteWing
            viscousWall  = {"boundaryLayerSpacing" : 0.001}
            self.pointwiseAim.input.Mesh_Sizing = {"OML": viscousWall,
                    "Farfield": {"bcType":"Farfield"},
                    "Symmetry" : {"bcType" : "SymmetryZ"}}

        elif (self.mesh_style == "tetgen"):
            #egads fluid tesselation params for surface mesh
            #self.egadsFluidAim.input.Tess_Params = [1.0, 0.001, 0.1]
            self.egadsFluidAim.input.Tess_Params = [0.01, 0.001, 5]
            #self.egadsFluidAim.input.Edge_Point_Min = 2
            #self.egadsFluidAim.input.Edge_Point_Max = 50
            #self.egadsFluidAim.input.Tess_Params = [15, 0.1, 20]
            #self.egadsFluidAim.input.Edge_Point_Min = 5
            #self.egadsFluidAim.input.Edge_Point_Max = 30

            #tetgen solvers['flow'] = fun3d_interface(self.comm, )AIM mesh params
            self.tetgenAim.input.Preserve_Surf_Mesh = True
            self.tetgenAim.input["Surface_Mesh"].link(self.egadsFluidAim.output["Surface_Mesh"])
            self.tetgenAim.input.Mesh_Format = "AFLR3"

            #self.tetgenAim.input.Edge_Point_Min = 5
            #self.tetgenAim.input.Edge_Point_Max = 10

            #viscousWall  = {"boundaryLayerSpacing" : 0.001}
            #self.tetgenAim.input.Mesh_Sizing = {"OML": viscousWall,
            #        "Farfield": {"bcType":"Farfield"},
            #        "Symmetry" : {"bcType" : "SymmetryZ"}}

    def fun3dSettings(self):
        self.fun3dAim.input.Boundary_Condition = {"OML": {"bcType" : "Inviscid"},
                "Farfield": {"bcType":"Farfield"},
                "Symmetry" : "SymmetryZ"}

        #add thickDVs and geomDVs to caps
        DVdict = {}
        for DV in self.DVdict:
            dvname = DV["name"]
            if (DV["type"] == "shape"): #geomDV, add empty entry into DV dicts
                DVdict[dvname] = {}

        #input DVdict and DVRdict into tacsAim
        self.fun3dAim.input.Design_Variable = DVdict

        #fun3d design sensitivities settings
        self.fun3dAim.input.Design_SensFile = True
        self.fun3dAim.input.Design_Sensitivity = True

    def initF2F(self):
        #clear F2F from previous run
        if (self.iteration > 1): self.clearF2F()


        structDVs = []
        for DV in self.DVdict:
            if (DV["type"] == "struct"):
                structDVs.append(DV["value"])
        
        maximum_mass = 40.0 
        num_tacs_dvs = len(structDVs)

        # Set up the communicators
        n_procs = self.comm.Get_size()
        if (self.n_tacs_procs > n_procs): self.n_tacs_procs = n_procs

        world_rank = self.comm.Get_rank()
        if world_rank < self.n_tacs_procs:
            color = 55
            key = world_rank
        else:
            color = MPI.UNDEFINED
            key = world_rank
        tacs_comm = self.comm.Split(color,key)

        #==================================================================================================#
        # Originally _build_model()

        # Build the model
        self.model = FUNtoFEMmodel('NACA Wing Simulation')
        self.wing = Body('wing', analysis_type=self.analysis_type, group=0,boundary=1)

        for i in range(num_tacs_dvs):
            self.wing.add_variable('structural',Variable('thickness '+ str(i),value=structDVs[i],lower = 0.0001, upper = 0.01))

        self.model.add_body(self.wing)

        steady = Scenario('steady', group=0, steps=5)
        
        self.functionNames = ["ksfailure"]

        function1 = Function('ksfailure',analysis_type='structural')
        steady.add_function(function1)

        #function2 = Function('cl', analysis_type='aerodynamic')
        #steady.add_function(function2)

        #function3 = Function('cd', analysis_type='aerodynamic')
        #steady.add_function(function3)

        #function2 = Function('mass',analysis_type='structural', adjoint=False) #,adjoint=False
        #steady.add_function(function2)

        self.model.add_scenario(steady)

        #self.cwrite("setup model, ")

        #==================================================================================================#

        solvers = {}
        solvers['flow'] = Fun3dInterface(self.comm,self.model,flow_dt=1.0)
        #self.cwrite("setup fun3d interface, ")

        datFile = os.path.join(self.curDir,"steady","Flow",self.mesh_style + ".dat")
        solvers['structural'] = wedgeTACS(self.comm,tacs_comm,self.model,self.n_tacs_procs, datFile)
        #self.cwrite("setup tacs interface\n")

        # L&D transfer options
        transfer_options = {'analysis_type': self.analysis_type,
                            'scheme': 'meld', 'thermal_scheme': 'meld'}

        # instantiate the driver
        self.driver = FUNtoFEMnlbgs(solvers,self.comm,tacs_comm,0,self.comm,0,transfer_options,model=self.model)
        struct_tacs = solvers['structural'].assembler
        #self.cwrite("\t setup adjoint driver, ")

        #section to update funtofem DV values for next fun3d run
        self.model.set_variables(structDVs)

    def clearF2F(self):
        #clear previous funtofem data and destroy it
        if (self.comm.Get_rank() == 0):
            delattr(self, "model")
            delattr(self, "driver")
            delattr(self, "wing")

    def forwardAnalysis(self):
        #update status
        self.cwrite("----------------------------")
        self.cwrite("----------------------------\n")
        #self.cwrite("Iteration #{}\n".format(self.iteration))

        #set design variables from desvarDict
        self.updateDesign()
        # thickDVstr = "[rib,spar,OML]"
        # shapeDVstr = "[area,aspect,camb0,cambf,ctwist,dihedral,lesweep,taper,tc0,tcf]"
        # self.cwrite("thickDVs {}\n\t{}\nshapeDVs {}\n\t{}\n".format(thickDVstr, x["struct"],shapeDVstr, x["shape"]))

        #generate structure mesh with egads and tacs AIMs
        self.buildStructureMesh()
        self.cwrite("built structure mesh\n")

        #generate fluid mesh with Pointwise
        self.buildFluidMesh()
        self.cwrite("built fluid mesh\n")

        #initialize fun2fem with new meshes
        #self.cwrite("Initializing funtofem... ")
        self.initF2F()
        #self.cwrite("initialized funtofem\n")

        #run FUNtoFEM forward analysis
        #run funtofem forward analysis, including fun3d
        self.cwrite("Running F2F forward analysis... ")
        self.driver.solve_forward()

        #get function values from forward analysis
        self.functions = self.model.get_functions()

        self.cwrite("completed F2F forward analysis\n")

    def adjointAnalysis(self):
        #update status
        self.cwrite("Running F2F adjoint analysis... ")

        #run funtofem adjoint analysis, with fun3d
        self.driver.solve_adjoint()

        self.commBarrier("ran adjoint")


        #update status
        self.cwrite("finished adjoint analysis\n")

        #get the funtofem gradients
        f2fgrads = self.model.get_function_gradients()
        
        #self.commBarrier("function_gradients()")

        #update gradient for non shape DVs
        nfunc = len(self.functions)
        structGrad = np.zeros((nfunc, 3))
        
        for funcInd in range(nfunc):
            ct = 0
            for DV in self.DVdict:
                dvname = DV["name"]
                if (not(DV["type"] == "shape")): 
                    structGrad[funcInd, ct] = f2fgrads[funcInd][ct].real
                    ct += 1
        
        #store structGrad
        self.structGrad = structGrad


        #get aero and struct mesh sensitivities for shape DVs
        self.aeroIds, self.aero_mesh_sens = self.wing.collect_coordinate_derivatives(self.comm, "aero")
        self.structIds, self.struct_mesh_sens = self.wing.collect_coordinate_derivatives(self.comm, "struct")

        if (self.comm.Get_rank() == 0):

            #if (oneBased): self.aeroIds = self.aeroIds - (min(self.aeroIds) - 1)
            print("aero ids: {}".format(self.aeroIds))
            print("aero mesh: {}".format(self.aero_mesh_sens))
            print("struct ids: {}".format(self.structIds))
            print("struct mesh: {}".format(self.struct_mesh_sens))

            #compute shape derivatives from aero and struct mesh sensitivities
            self.shapeGrad = self.computeShapeDerivatives()
            self.cwrite("computed shape derivative chain rule products\n")

        else:
            self.shapeGrad = None

        #barrier to wait for root proc to finish computing the shape derivatives
        self.commBarrier("computeShapeDerivatives()")

        self.shapeGrad = self.comm.bcast(self.shapeGrad, root=0)

        #send or store gradient
        #return structGrad, self.shapeGrad

    def updateDesign(self):
        
        #update shapeDVs in each caps problem
        #update thickness design variables in tacsAim
        if (self.comm.Get_rank() == 0):
            #grab the dictionaries in tacsAim
            propDict = self.tacsAim.input.Property
            DVRdict = self.tacsAim.input.Design_Variable_Relation
            DVdict = self.tacsAim.input.Design_Variable

            #update thickness design variables in caps aims
            thickCt = 0
            thickVec = []
            for DV in self.DVdict:
                dvname = DV["name"]
                value = DV["value"]
                if (DV["type"] == "shape"):

                    #update both of the capsStruct and capsFluid geometries
                    self.capsStruct.geometry.despmtr[dvname].value = value
                    self.capsFluid.geometry.despmtr[dvname].value = value

                elif (DV["type"] == "struct"):

                    #update the thickness DV in its tacsAim dictionaries
                    DVRdict[dvname] = self.makeThicknessDVR(dvname)
                    capsGroup = DVdict[dvname]["groupName"]
                    DVdict[dvname] = self.makeThicknessDV(capsGroup,value)
                    propDict[capsGroup]["membraneThickness"] = value

                    #update funtofem thickness vec
                    thickVec.append(value)

            #update tacsAim dictionaries
            self.tacsAim.input.Property = propDict
            self.tacsAim.input.Design_Variable_Relation = DVRdict
            self.tacsAim.input.Design_Variable = DVdict
        
    def makeThicknessDV(self, capsGroup, thickness):
        #thick DV dictionary for Design_Variable Dict
        desvar    = {"groupName" : capsGroup,
                "initialValue" : thickness,
                "lowerBound" : thickness*0.5,
                "upperBound" : thickness*1.5,
                "maxDelta"   : thickness*0.1}
        return desvar
        
    def makeThicknessDVR(self, DVname):
        #thick DV dictionary for Design_Variable_Relation Dict
        DVR = {"variableType": "Property",
        "fieldName" : "T",
        "constantCoeff" : 0.0,
        "groupName" : DVname,
        "linearCoeff" : 1.0}
        return DVR           

    def buildStructureMesh(self):
        self.cwrite("Building structure mesh... ")
        #build structure mesh by running tacsAim preanalysis
        if (self.comm.Get_rank() == 0):
            self.tacsAim.preAnalysis()

            #move struct mesh files to flow directory 
            for extension in [".bdf",".dat"]:
                src = os.path.join(self.tacsAim.analysisDir, self.tacsAim.input.Proj_Name+extension)
                dest = os.path.join(self.curDir,"steady","Flow",self.tacsAim.input.Proj_Name+extension)
                shutil.copy(src, dest)

    def buildFluidMesh(self):
        self.cwrite("Building fluid mesh... ")

        if (self.comm.Get_rank() == 0):
            if (self.mesh_style == "pointwise"):
                #build fluid mesh by running pointwise and then linking with fun3d
                self.runPointwise()
                self.cwrite("ran pointwise, ")


            #update fun3d with current mesh so it knows mesh sensitivity
            if (self.mesh_style == "pointwise"):
                self.fun3dAim.input["Mesh"].link(self.pointwiseAim.output["Volume_Mesh"])
            elif (self.mesh_style == "tetgen"):
                self.fun3dAim.input["Mesh"].link(self.tetgenAim.output["Volume_Mesh"])
            self.fun3dAim.preAnalysis()
            self.cwrite("linked to fun3d, ")

            #move ugrid file to fun3d run directory steady/Flow
            for ext in [".ugrid", ".lb8.ugrid"]:
                if (self.mesh_style == "pointwise"):
                    srcFile = "caps.GeomToMesh" + ext
                    destFile = "pointwise" + ext
                    src = os.path.join(self.pointwiseAim.analysisDir, srcFile)
                elif (self.mesh_style == "tetgen"):
                    if (".lb8" in ext):
                        #might also need to move the fun3d.mapbc file for this one
                        srcFile = "fun3d_CAPS" + ext
                        destFile = "tetgen" + ext
                        src = os.path.join(self.fun3dAim.analysisDir, "Flow", srcFile)
                
            dest = os.path.join(self.curDir, "steady", "Flow", destFile)
            shutil.copy(src, dest)

            #if tetgen also move the mapbc file
            if (self.mesh_style == "tetgen"):
                src = os.path.join(self.fun3dAim.analysisDir, "Flow", "fun3d_CAPS.mapbc")
                dest = os.path.join(self.curDir, "steady", "Flow", "tetgen.mapbc")
                shutil.copy(src, dest)

    def runPointwise(self):
        #run AIM pre-analysis
        self.pointwiseAim.preAnalysis()

        #move to test directory
        self.curDir = os.getcwd()
        os.chdir(self.pointwiseAim.analysisDir)

        CAPS_GLYPH = os.environ["CAPS_GLYPH"]
        for i in range(1): #can run extra times if having license issues
                os.system("pointwise -b " + CAPS_GLYPH + "/GeomToMesh.glf caps.egads capsUserDefaults.glf")
            #if os.path.isfile('caps.GeomToMesh.gma') and os.path.isfile('caps.GeomToMesh.ugrid'): break

        os.chdir(self.curDir)

        #run AIM postanalysis, files in self.pointwiseAim.analysisDir
        self.pointwiseAim.postAnalysis()      


    def runF2F(self):

        #run forward analysis
        self.forwardAnalysis()        

        #run adjoint analysis
        self.adjointAnalysis()

        #write to output
        self.writeOutput()

    def writeOutput(self):
        #...write the output functions, gradients, etc

        curDir = os.getcwd()
        #print(curDir)
        funtofemFolder = os.path.join(curDir, "funtofem")

        outputFile = os.path.join(funtofemFolder, "funtofem.out")
        outputHandle =  open(outputFile, "w")

        #write functions
        if (self.comm.Get_rank() == 0):

            #count number of shape and struct DV
            nshape = 0
            nstruct = 0
            for DV in self.DVdict:
                if (DV["type"] == "shape"): nshape += 1
                if (DV["type"] == "struct"): nstruct += 1

            #write number of functions, variables, etc.
            outputHandle.write("{},{},{}\n".format(self.nfunc, nshape, nstruct))
            ct = 0
            for func in self.functions:

                name = self.functionNames[ct]
                value = self.functions[ct].value.real
                ct += 1

                #write function name and value
                outputHandle.write("func,{},{}\n".format(name,value))

                #write shape gradients
                outputHandle.write("grad,shape")

                for ishape in range(nshape):
                    deriv = self.shapeGrad[ct, ishape]
                    outputHandle.write(",{}".format(deriv))
                
                outputHandle.write("\n")

                #write struct gradients
                outputHandle.write("grad,struct")

                for istruct in range(nstruct):
                    deriv = self.structGrad[ct, istruct]
                    outputHandle.write(",{}".format(deriv))
                
                outputHandle.write("\n")

        outputHandle.close()

    def computeShapeDerivatives(self):
        self.cwrite("Compute shape derivatives... ")

        if (self.comm.Get_rank() == 0):
            #initialize shape gradient again at zero
            self.initShapeGrad()

            #add struct_mesh_sens part to shape DV derivatives#struct shape derivatives
            self.applyStructMeshSens()

            #add aero_mesh_sens part to shape DV derivatives
            self.applyAeroMeshSens()

        return self.shapeGrad

    def initShapeGrad(self):
        self.nfunc = len(self.functions)

        #determine number of shapeDV
        self.nshapeDV = 0
        for DV in self.DVdict:
            if (DV["type"] == "shape"): self.nshapeDV += 1

        self.shapeGrad = np.zeros((self.nfunc, self.nshapeDV))

        self.cwrite("initialized shape gradient\n")

    def applyStructMeshSens(self):
        #print struct mesh sens to struct mesh sens file
        #where to print .sens file
        structSensFile = os.path.join(self.tacsAim.analysisDir, self.tacsAim.input.Proj_Name+".sens")

        #open the file
        with open(structSensFile, "w") as f:
            
            #write (nfunctions) in first line
            f.write("{}\n".format(self.nfunc))
            
            #for each function mass, stress, etc.
            for funcInd in range(self.nfunc):
                
                #get the pytacs/tacs sensitivity w.r.t. mesh for that function
                sens = self.struct_mesh_sens

                #write the key,value,nnodes of the function
                funcKey = "func#" + str(funcInd)
                f.write(funcKey + "\n")
                f.write("{}\n".format(self.functions[funcInd].value.real))
                f.write("{}\n".format(len(self.structIds)))

                #for each node, print nodeind, dfdx, dfdy, dfdz for that mesh element
                ct = 0
                for nodeind in self.structIds: # d(Func1)/d(xyz)
                    bdfind = nodeind + 1
                    f.write("{} {} {} {}\n".format(bdfind, sens[3*ct, funcInd].real, sens[3*ct+1, funcInd].real, sens[3*ct+2, funcInd].real))
                    ct += 1
        
            f.close()

        #update status
        self.cwrite("\tprinted struct.sens file, ")

        #run aim postanalysis
        self.tacsAim.postAnalysis()
        self.cwrite("completed tacsAim postAnalysis(), ")

        #update shape DV derivatives from struct mesh part
        for funcInd in range(self.nfunc):
            funcKey = "func#" + str(funcInd)
            dvct = 0
            for DV in self.DVdict:
                dvname = DV["name"]
                if (DV["type"] == "shape"): 
                    self.shapeGrad[funcInd, dvct] += self.tacsAim.dynout[funcKey].deriv(dvname)
                    dvct += 1

        #update status
        self.cwrite("finished struct mesh contribution to shape DVs\n")


    def applyAeroMeshSens(self):
        #print aero mesh sens to aero mesh sens file
        #where to print .sens file
        aeroSensFile = os.path.join(self.fun3dAim.analysisDir, self.fun3dAim.input.Proj_Name+".sens")
        #print("Writing aero sens file, {}".format(aeroSensFile))

        #make aero mesh derivatives (surface aero mesh) one based
        aero_nnodes = len(self.aeroIds)
        #minId = min(self.aeroIds)
        #maxId = max(self.aeroIds)
        
        #open the file
        with open(aeroSensFile, "w") as f:
            
            #write (nfunctions) in first line
            f.write("{}\n".format(self.nfunc))
            
            #for each function mass, stress, etc.
            for funcInd in range(self.nfunc):
                
                #get the pytacs/tacs sensitivity w.r.t. mesh for that function
                sens = self.aero_mesh_sens

                #write the key,value,nnodes of the function
                funcKey = "func#" + str(funcInd)
                f.write(funcKey + "\n")
                f.write("{}\n".format(self.functions[funcInd].value.real))
                
                #print aero_nnodes on aerodynamic surface mesh
                aero_nnodes = len(self.aeroIds)
                f.write("{}\n".format(aero_nnodes))

                #for each node, print nodeind, dfdx, dfdy, dfdz for that mesh element
                ct = 0
                for nodeind in self.aeroIds: # d(Func1)/d(xyz)
                    #bdfind = nodeind - (minId - 1)
                    bdfind = nodeind
                    f.write("{} {} {} {}\n".format(bdfind, sens[3*ct, funcInd].real, sens[3*ct+1, funcInd].real, sens[3*ct+2, funcInd].real))
                    ct += 1

            f.close()

        #update status
        self.cwrite("\tprinted aero.sens file, ")

        #run aim postanalysis
        self.fun3dAim.postAnalysis()
        self.cwrite("completed pointwiseAim postAnalysis(), ")

        #update shape DV derivatives from aero mesh part
        for funcInd in range(self.nfunc):
            funcKey = "func#" + str(funcInd)
            dvct = 0
            for DV in self.DVdict:
                dvname = DV["name"]
                if (DV["type"] == "shape"): 
                    self.shapeGrad[funcInd, dvct] += self.fun3dAim.dynout[funcKey].deriv(dvname)
                    dvct += 1
            
        #update status
        self.cwrite("finished aero mesh contribution to shape DVs\n")


##----------Outside of class, run cases------------------##

#call the class and initialize it
comm = MPI.COMM_WORLD

#INIT and read in inputs
nacaOpt = NacaOMLOptimization(comm, "naca_OML_struct.csm", "naca_OML_fluid.csm", "pointwise", "aerothermoelastic")

#RUN analysis
nacaOpt.runF2F()

#write output
nacaOpt.writeOutput()
