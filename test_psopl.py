from pyocse.pso import PSO
from pyocse.parameters import ForceFieldParametersBase
from time import time
import os
import numpy as np
from ase import units
from lammps import PyLammps
import numpy as np
import multiprocessing as mp

# Helper: Initialize global variables for each worker
def worker_init(shared_template, shared_ref_data, shared_e_offset):
    global template, ref_data, e_offset
    template = shared_template
    ref_data = shared_ref_data
    e_offset = shared_e_offset

# Worker function for a single parameter evaluation
def worker(para_values):
    """
    Evaluate the objective function for a single parameter set.
    """
    score = obj_function(para_values, template, ref_data, e_offset, obj="R2")
    return score

def execute_lammps(lmp_in, N_strucs):
    """
    Args:
        lmp_in (str): path of lmp_in
        N_strucs (int): Number of structures
    """
    cmdargs = ["-screen", "none", "-log", "none", "-nocite"]
    lmp = PyLammps(cmdargs = cmdargs)
    engs = []
    for id in range(N_strucs):
        lmp.command(f"variable index equal {id+1}")
        lmp.file(lmp_in)
        lmp.run(0)
        thermo = lmp.last_run[-1]
        energy = float(thermo.TotEng[-1]) * units.kcal/units.mol
        stress = np.zeros(6)
        # traditional Voigt order (xx, yy, zz, yz, xz, xy)
        stress_vars = ['pxx', 'pyy', 'pzz', 'pyz', 'pxz', 'pxy']
        for i, var in enumerate(stress_vars):
            stress[i] = lmp.variables[var].value

        fx = np.frombuffer(lmp.variables['fx'].value)
        fy = np.frombuffer(lmp.variables['fy'].value)
        fz = np.frombuffer(lmp.variables['fz'].value)

        stress = -stress * 101325 * units.Pascal
        force = np.vstack((fx, fy, fz)).T.flatten() * units.kcal/units.mol
        engs.append(energy)
        if id > 0:
            stresses = np.append(stresses, stress)
            forces = np.append(forces, force)
        else:
            stresses = stress
            forces = force
        #print(id, len(force), len(forces))
    lmp.close()

    engs = np.array(engs)
    return engs, forces, stresses


def r2_score(y_true, y_pred):
    tss = np.sum((y_true - np.mean(y_true))**2)
    rss = np.sum((y_true - y_pred)**2)
    r2 = 1 - (rss / tss)
    return r2

def get_force_arr(input_file):
    # Initialize lists to store data
    forces = []

    # Read the forces_summary.txt file
    with open(input_file, "r") as file:
        for line in file:
            # Skip non-data lines (assuming format: id type fx fy fz)
            if line.strip() and not line.startswith("ITEM"):
                p = line.split()
                if len(p) == 5:  # Ensure the line contains data
                    forces.append(float(p[2]))
                    forces.append(float(p[3]))
                    forces.append(float(p[4]))
    forces = np.array(forces)
    return forces.flatten()

def obj_function_par(para_values_list, template, ref_data, e_offset, ncpu):
    if ncpu == 1:  # No parallelization
        return [obj_function(vals, template, ref_data, e_offset, obj="R2") for vals in para_values_list]

    # Initialize a multiprocessing pool
    with mp.Pool(
        processes=ncpu,
        initializer=worker_init,
        initargs=(template, ref_data, e_offset)
    ) as pool:
        # Map the computation across workers
        results = pool.map(worker, para_values_list)
    print(f"Evaluating {len(para_values_list)} parameter sets using {ncpu} CPUs.")

    return results

#@timeit
def obj_function(para_value, template, ref_data, e_offset, obj='R2'):
    """
    Objective function for the PSO Optimizer.

    Args:
        para_value: 1D-Array of parameter values to evaluate.
        template: A dictionary to guide the order of coefficients
        ref_data: (eng, force, stress, numbers, mask_e, mask_f, mask_s)

    Returns:
        Objective score.
    """
    cpu_id = (mp.current_process()._identity[0] - 1) % mp.cpu_count() if mp.current_process()._identity else 0
    if cpu_id < 10:
        folder = f"par/cpu00{cpu_id}"
    elif cpu_id < 100:
        folder = f"par/cpu0{cpu_id}"
    else:
        folder = f"par/cpu{cpu_id}"
    os.makedirs(folder, exist_ok=True)
    lmp_in_file = os.path.join(folder, "lmp.in")
    strs = f"""
clear

units real
atom_style full

dimension 3
boundary p p p  # p p m for periodic and tilt box
atom_modify sort 0 0.0

bond_style hybrid harmonic
angle_style hybrid harmonic
dihedral_style hybrid charmm
special_bonds amber lj 0.0 0.0 0.5 coul 0.0 0.0 0.83333 angle yes dihedral no

neighbor 2.0 multi
neigh_modify every 2 delay 4 check yes

# Pair Style
pair_style lj/cut/coul/long 9.0 9.0
pair_modify mix arithmetic shift no tail yes

box tilt large

# Read unique Atoms and Box for the current structure
variable atomfile string structures/lmp_dat_${{index}}
read_data ${{atomfile}} #add append
"""
    # Dynamically add mass entries
    for key in sorted(template):
        if key.startswith("mass"):
            atom_type = key.split()[1]
            mass_val = template[key]
            strs += f"mass {atom_type} {mass_val:.8f}\n"

    for key in template.keys():
        items = template[key]
        if key.startswith('bond_coeff'):
            [id1, id2] = items
            strs += f'{key} harmonic {para_value[id1]} {para_value[id2]}\n'
        elif key.startswith('angle_coeff'):
            [id1, id2] = items
            strs += f'{key} harmonic {para_value[id1]} {para_value[id2]}\n'
        elif key.startswith('dihedral_coeff'):
            [id1, arg0, arg1, arg2] = items
            strs += f'{key} charmm {para_value[id1]} {arg0} {arg1} {arg2}\n'
        elif key.startswith('pair_coeff'):
            [id1, id2] = items
            fact = 2**(5/6)
            strs += f'{key} {para_value[id1]} {para_value[id2]*fact}\n'

    strs += f"""
# dynamically specify the mesh grid based on the box
# Read box dimensions
variable xdim equal xhi-xlo
variable ydim equal yhi-ylo
variable zdim equal zhi-zlo
# Set minimum FFT grid size
variable xfft equal ceil(${{xdim}})
variable yfft equal ceil(${{ydim}})
variable zfft equal ceil(${{zdim}})
# Apply a minimum grid size of 32
if "${{xfft}} < 32" then "variable xfft equal 32"
if "${{yfft}} < 32" then "variable yfft equal 32"
if "${{zfft}} < 32" then "variable zfft equal 32"
kspace_style pppm 0.0005
kspace_modify gewald 0.29202898720871845 mesh ${{xfft}} ${{yfft}} ${{zfft}} order 6

# Thermodynamic Variables
variable pxx equal pxx
variable pyy equal pyy
variable pzz equal pzz
variable pyz equal pyz
variable pxz equal pxz
variable pxy equal pxy
variable fx atom fx
variable fy atom fy
variable fz atom fz
"""

    e_coef = 1
    f_coef = 1
    s_coef = 1
    with open(f"{lmp_in_file}", 'w') as f: f.write(strs)
    engs1, forces1, stress1 = execute_lammps(f"{lmp_in_file}", len(ref_data[0]))

    # Reference data
    (engs, forces, stress, numbers, mask_e, mask_f, mask_s) = ref_data
    engs0 = engs / numbers
    engs0 = engs0[mask_e]
    forces0 = forces[mask_f]
    stress0 = stress[mask_s]

    # FF data
    engs1 /= numbers
    engs1 = engs1[mask_e]
    engs1 += e_offset
    forces1 = forces1[mask_f]
    stress1 = stress1[mask_s]

    # Calculate objective score
    if obj == "MSE":
        score = e_coef * np.sum((engs1 - engs0) **2)
        score += f_coef * np.sum((forces1 - forces0) ** 2)
        score += s_coef * np.sum((stress1 - stress0) ** 2)
    elif obj == "R2":
        score = -r2_score(engs1, engs0)#; print('eng', engs1, engs0, r2_score(engs1, engs0))
        score -= r2_score(forces1, forces0)
        score -= r2_score(stress1, stress0)
    return score


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--ncpu", type=int, default=10,
                        help="Number of CPUs to use, default is 5.")
    parser.add_argument("--steps", type=int, default=30,
                        help="Number of opt steps, default is 30.")
    parser.add_argument("--ref", default="dataset/references.xml",
                        help="Reference dataset file, default is dataset/references.xml.")
    parser.add_argument("--params", default="dataset/parameters.xml",
                        help="Initial parameters file, default is dataset/parameters.xml.")
    parser.add_argument("--style", default="openff",
                        help="Force field style, default is openff.")
    parser.add_argument("--export", default="parameters_opt_pso.xml",
                        help="Export optimized parameters, default is parameters_opt_pso.xml.")
    parser.add_argument("--dir", default="mtest",
                        help="Output directory, default is test.")

    args = parser.parse_args()

    np.random.seed(1234)
    params = ForceFieldParametersBase(
        smiles=['CC(=O)OC1=CC=CC=C1C(=O)O'],
        f_coef=1, #0.1,
        s_coef=1, #0,
        e_coef=1,
        style=args.style,
        ncpu=1,
    )

    p0, errors = params.load_parameters(args.params)
    ref_dics = params.load_references(args.ref)[:20]

    os.makedirs(args.dir, exist_ok=True)
    os.chdir(args.dir)

    # Prepare lmp.dat at once
    params.write_lmp_dat_from_ref_dics(ref_dics)

    e_offset, params_opt = params.optimize_offset(ref_dics, p0)
    params.update_ff_parameters(params_opt)
    errs = params.plot_ff_results("performance_init.png", ref_dics, [params_opt])
    t0 = time()
    print("R2 objective", params.get_objective(ref_dics, e_offset, obj="R2"))
    os.system("mv lmp.in lmp0.in")
    t1 = time(); print("computation from params", t1-t0)

    ref_data = params.get_reference_data_and_mask(ref_dics)

    # Stepwise optimization loop
    terms = ["bond", "angle", "proper", "vdW"]

    sub_vals, sub_bounds, _ = params.get_sub_parameters(params_opt, terms)
    vals = np.concatenate(sub_vals)
    bounds = np.concatenate(sub_bounds)

    # PSO-GA optimization
    optimizer = PSO(
            obj_function=obj_function_par,
            obj_args=(TEMPLATE, ref_data, e_offset, args.ncpu),
            bounds=bounds,
            seed=vals.reshape((1, len(vals))),
            num_particles=5,
            dimensions=len(bounds),
            inertia=0.5,
            cognitive=0.2,
            social=0.8,
            max_iter=5, #args.steps,
            ncpu=5, #args.ncpu,
            xml_file="pso.xml",
    )

    best_position, best_score = optimizer.optimize()

    params_opt = params.set_sub_parameters(best_position, terms, params_opt)
    e_offset, params_opt = params.optimize_offset(ref_dics, params_opt)
    params.update_ff_parameters(params_opt)
    print("e_offset", e_offset)

    t = (time() - t0) / 60
    print(f"\nStepwise optimization for terms {terms} completed in {t:.2f} minutes.")
    print(f"Best Score: {best_score:.4f}")

    # Final evaluation and saving results
    errs = params.plot_ff_results("performance_opt_pso.png", ref_dics, [params_opt])
    params.export_parameters("parameters_opt_pso.xml", params_opt, errs[0])
    print("Optimization completed successfully.")

    optimizer = PSO.load("pso.xml", obj_function_par, (template, ref_data, e_offset, args.ncpu))
    print(optimizer)
    print(optimizer.global_best_id, optimizer.global_best_score)

