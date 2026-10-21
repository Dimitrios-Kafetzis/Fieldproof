#include "world_events.hpp"

#include "fieldproof_gz_plugins/version.hpp"

#include <gz/common/Console.hh>
#include <gz/math/Pose3.hh>
#include <gz/math/Quaternion.hh>
#include <gz/math/Vector3.hh>
#include <gz/msgs/Utility.hh>
#include <gz/plugin/Register.hh>
#include <gz/sim/SdfEntityCreator.hh>
#include <gz/sim/World.hh>
#include <gz/sim/components/LinearVelocity.hh>
#include <gz/sim/components/LinearVelocitySeed.hh>
#include <gz/sim/components/Model.hh>
#include <gz/sim/components/Name.hh>
#include <gz/sim/components/ParentEntity.hh>
#include <gz/sim/components/Pose.hh>
#include <gz/sim/components/PoseCmd.hh>
#include <gz/sim/components/Wind.hh>
#include <sdf/Error.hh>
#include <sdf/Model.hh>
#include <sdf/Root.hh>

#include <sstream>
#include <utility>

namespace fieldproof {

namespace {

std::string Error(const std::string& message) {
  return "error: " + message;
}

std::string Describe(const gz::math::Pose3d& pose) {
  std::ostringstream out;
  out << "(" << pose.Pos().X() << ", " << pose.Pos().Y() << ", " << pose.Pos().Z() << ", yaw "
      << pose.Rot().Yaw() << ")";
  return out.str();
}

std::string Describe(const gz::math::Vector3d& v) {
  std::ostringstream out;
  out << "(" << v.X() << ", " << v.Y() << ", " << v.Z() << ")";
  return out.str();
}

/// The pose of a message, with an all-zero quaternion read as no rotation.
gz::math::Pose3d PoseOf(const gz::msgs::Pose& msg) {
  gz::math::Pose3d pose = gz::msgs::Convert(msg);
  const auto& q = msg.orientation();
  if (q.w() == 0.0 && q.x() == 0.0 && q.y() == 0.0 && q.z() == 0.0) {
    pose.Rot() = gz::math::Quaterniond::Identity;
  }
  return pose;
}

}  // namespace

void WorldEvents::Configure(const gz::sim::Entity& entity,
                            const std::shared_ptr<const sdf::Element>& /*sdf*/,
                            gz::sim::EntityComponentManager& ecm,
                            gz::sim::EventManager& event_mgr) {
  world_entity_ = entity;
  event_manager_ = &event_mgr;
  const gz::sim::World world(entity);
  const auto name = world.Name(ecm);
  if (!name) {
    gzerr << "[" << fieldproof_gz_plugins::Banner()
          << "] WorldEvents must be attached to a world\n";
    return;
  }
  world_name_ = *name;
  bool ok = node_.Advertise(ServiceName(world_name_, "spawn"), &WorldEvents::OnSpawn, this);
  ok = node_.Advertise(ServiceName(world_name_, "remove"), &WorldEvents::OnRemove, this) && ok;
  ok = node_.Advertise(ServiceName(world_name_, "teleport"), &WorldEvents::OnTeleport, this) && ok;
  ok = node_.Advertise(ServiceName(world_name_, "set_wind"), &WorldEvents::OnSetWind, this) && ok;
  wind_publisher_ = node_.Advertise<gz::msgs::Wind>("/world/" + world_name_ + "/wind");
  if (!ok) {
    gzerr << "[" << fieldproof_gz_plugins::Banner() << "] WorldEvents could not advertise its "
          << "services for world [" << world_name_ << "]\n";
    return;
  }
  gzmsg << "[" << fieldproof_gz_plugins::Banner() << "] WorldEvents serving "
        << ServiceName(world_name_, "{spawn,remove,teleport,set_wind}") << "\n";
}

void WorldEvents::PreUpdate(const gz::sim::UpdateInfo& /*info*/,
                            gz::sim::EntityComponentManager& ecm) {
  std::vector<std::shared_ptr<PendingRequest>> batch;
  {
    const std::lock_guard<std::mutex> lock(mutex_);
    batch.swap(pending_);
  }
  if (batch.empty()) {
    return;
  }
  for (const auto& request : batch) {
    const std::string result = request->apply(ecm);
    const std::lock_guard<std::mutex> lock(mutex_);
    request->result = result;
    request->done = true;
  }
  applied_.notify_all();
}

bool WorldEvents::Submit(std::function<std::string(gz::sim::EntityComponentManager&)> apply,
                         gz::msgs::StringMsg& reply) {
  auto request = std::make_shared<PendingRequest>();
  request->apply = std::move(apply);
  std::unique_lock<std::mutex> lock(mutex_);
  pending_.push_back(request);
  const bool done = applied_.wait_for(lock, kApplyTimeout, [&] { return request->done; });
  if (!done) {
    reply.set_data(Error("the simulation did not step within " +
                         std::to_string(kApplyTimeout.count()) + " s"));
    return true;
  }
  reply.set_data(request->result);
  return true;
}

bool WorldEvents::OnSpawn(const gz::msgs::EntityFactory& request, gz::msgs::StringMsg& reply) {
  return Submit(
      [this, request](gz::sim::EntityComponentManager& ecm) { return Spawn(request, ecm); }, reply);
}

bool WorldEvents::OnRemove(const gz::msgs::Entity& request, gz::msgs::StringMsg& reply) {
  return Submit(
      [this, request](gz::sim::EntityComponentManager& ecm) { return Remove(request, ecm); },
      reply);
}

bool WorldEvents::OnTeleport(const gz::msgs::Pose& request, gz::msgs::StringMsg& reply) {
  return Submit(
      [this, request](gz::sim::EntityComponentManager& ecm) { return Teleport(request, ecm); },
      reply);
}

bool WorldEvents::OnSetWind(const gz::msgs::Wind& request, gz::msgs::StringMsg& reply) {
  return Submit(
      [this, request](gz::sim::EntityComponentManager& ecm) { return SetWind(request, ecm); },
      reply);
}

gz::sim::Entity WorldEvents::ModelByName(const std::string& name,
                                         const gz::sim::EntityComponentManager& ecm) const {
  return ecm.EntityByComponents(gz::sim::components::Name(name),
                                gz::sim::components::ParentEntity(world_entity_),
                                gz::sim::components::Model());
}

std::string WorldEvents::Spawn(const gz::msgs::EntityFactory& request,
                               gz::sim::EntityComponentManager& ecm) {
  sdf::Root root;
  sdf::Errors errors;
  switch (request.from_case()) {
    case gz::msgs::EntityFactory::kSdf:
      errors = root.LoadSdfString(request.sdf());
      break;
    case gz::msgs::EntityFactory::kSdfFilename:
      errors = root.Load(request.sdf_filename());
      break;
    default:
      return Error("spawn: the request carries neither sdf nor sdf_filename");
  }
  if (!errors.empty()) {
    std::ostringstream out;
    out << "spawn: the SDF does not parse:";
    for (const auto& error : errors) {
      out << " " << error.Message();
    }
    return Error(out.str());
  }
  const sdf::Model* model = root.Model();
  if (model == nullptr) {
    return Error("spawn: the SDF holds no model");
  }
  sdf::Model copy = *model;
  std::string name = request.name().empty() ? copy.Name() : request.name();
  if (name.empty()) {
    return Error("spawn: the model has no name");
  }
  if (ModelByName(name, ecm) != gz::sim::kNullEntity) {
    if (!request.allow_renaming()) {
      return Error("spawn: a model called " + name + " already exists in world " + world_name_);
    }
    int suffix = 0;
    while (ModelByName(name + "_" + std::to_string(suffix), ecm) != gz::sim::kNullEntity) {
      ++suffix;
    }
    name += "_" + std::to_string(suffix);
  }
  copy.SetName(name);
  gz::sim::SdfEntityCreator creator(ecm, *event_manager_);
  const gz::sim::Entity entity = creator.CreateEntities(&copy);
  creator.SetParent(entity, world_entity_);
  gz::math::Pose3d pose = copy.RawPose();
  if (request.has_pose()) {
    pose = PoseOf(request.pose());
    auto* component = ecm.Component<gz::sim::components::Pose>(entity);
    if (component != nullptr) {
      *component = gz::sim::components::Pose(pose);
    } else {
      ecm.CreateComponent(entity, gz::sim::components::Pose(pose));
    }
  }
  gzmsg << "[fieldproof] spawned model " << name << " at " << Describe(pose) << "\n";
  return "spawned model " + name + " at " + Describe(pose);
}

std::string WorldEvents::Remove(const gz::msgs::Entity& request,
                                gz::sim::EntityComponentManager& ecm) {
  if (request.name().empty()) {
    return Error("remove: the request names no model");
  }
  const gz::sim::Entity entity = ModelByName(request.name(), ecm);
  if (entity == gz::sim::kNullEntity) {
    return Error("remove: no model called " + request.name() + " in world " + world_name_);
  }
  gz::sim::SdfEntityCreator creator(ecm, *event_manager_);
  creator.RequestRemoveEntity(entity);
  gzmsg << "[fieldproof] removed model " << request.name() << "\n";
  return "removed model " + request.name();
}

std::string WorldEvents::Teleport(const gz::msgs::Pose& request,
                                  gz::sim::EntityComponentManager& ecm) {
  if (request.name().empty()) {
    return Error("teleport: the request names no model");
  }
  const gz::sim::Entity entity = ModelByName(request.name(), ecm);
  if (entity == gz::sim::kNullEntity) {
    return Error("teleport: no model called " + request.name() + " in world " + world_name_);
  }
  const gz::math::Pose3d pose = PoseOf(request);
  auto* command = ecm.Component<gz::sim::components::WorldPoseCmd>(entity);
  if (command == nullptr) {
    ecm.CreateComponent(entity, gz::sim::components::WorldPoseCmd(pose));
  } else {
    command->Data() = pose;
    ecm.SetChanged(entity, gz::sim::components::WorldPoseCmd::typeId,
                   gz::sim::ComponentState::OneTimeChange);
  }
  gzmsg << "[fieldproof] teleported model " << request.name() << " to " << Describe(pose) << "\n";
  return "teleported model " + request.name() + " to " + Describe(pose);
}

std::string WorldEvents::SetWind(const gz::msgs::Wind& request,
                                 gz::sim::EntityComponentManager& ecm) {
  const gz::sim::Entity wind = ecm.EntityByComponents(gz::sim::components::Wind());
  if (wind == gz::sim::kNullEntity) {
    return Error("set_wind: world " + world_name_ + " has no wind entity");
  }
  const gz::math::Vector3d velocity = request.enable_wind()
                                          ? gz::msgs::Convert(request.linear_velocity())
                                          : gz::math::Vector3d::Zero;
  for (auto* seed : {ecm.Component<gz::sim::components::WorldLinearVelocitySeed>(wind)}) {
    if (seed == nullptr) {
      ecm.CreateComponent(wind, gz::sim::components::WorldLinearVelocitySeed(velocity));
    } else {
      seed->Data() = velocity;
      ecm.SetChanged(wind, gz::sim::components::WorldLinearVelocitySeed::typeId,
                     gz::sim::ComponentState::OneTimeChange);
    }
  }
  auto* current = ecm.Component<gz::sim::components::WorldLinearVelocity>(wind);
  if (current == nullptr) {
    ecm.CreateComponent(wind, gz::sim::components::WorldLinearVelocity(velocity));
  } else {
    current->Data() = velocity;
    ecm.SetChanged(wind, gz::sim::components::WorldLinearVelocity::typeId,
                   gz::sim::ComponentState::OneTimeChange);
  }
  // Gazebo's WindEffects system keeps its own copy (its `wind_info` service and the force it
  // applies to the links that enable wind) and reads it from the world's wind topic.
  gz::msgs::Wind message;
  message.set_enable_wind(request.enable_wind());
  gz::msgs::Set(message.mutable_linear_velocity(), velocity);
  wind_publisher_.Publish(message);
  if (!request.enable_wind()) {
    gzmsg << "[fieldproof] wind disabled\n";
    return "wind disabled";
  }
  gzmsg << "[fieldproof] wind set to " << Describe(velocity) << " m/s\n";
  return "wind set to " + Describe(velocity) + " m/s";
}

}  // namespace fieldproof

GZ_ADD_PLUGIN(fieldproof::WorldEvents, gz::sim::System, fieldproof::WorldEvents::ISystemConfigure,
              fieldproof::WorldEvents::ISystemPreUpdate)
GZ_ADD_PLUGIN_ALIAS(fieldproof::WorldEvents, "fieldproof::WorldEvents")
