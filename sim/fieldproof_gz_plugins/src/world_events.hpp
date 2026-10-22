// World mutation services for the event injection framework (task P2-T03, ADR-0005).
//
// `fieldproof::WorldEvents` is a world system plugin that advertises four gz-transport
// services under `/world/<world>/fieldproof/`:
//
//   spawn     gz.msgs.EntityFactory -> gz.msgs.StringMsg   a model from an SDF string or file,
//                                                          at a pose, with a name
//   remove    gz.msgs.Entity        -> gz.msgs.StringMsg   a model of the world, by name
//   teleport  gz.msgs.Pose          -> gz.msgs.StringMsg   move a model (name + pose)
//   set_wind  gz.msgs.Wind          -> gz.msgs.StringMsg   the world's wind velocity
//
// Gazebo's own UserCommands system offers `create`, `remove` and `set_pose`, but answers as
// soon as a request is queued: a client learns neither whether the request was valid (a name
// already taken, an SDF that does not parse, a model that does not exist) nor when it took
// effect. The scenario engine needs both, since a timeline entry ends `ok` or `failed` on the
// answer and the next entry (or an assertion window) may depend on the world having changed.
// So every request here is queued by the service thread, applied against the entity component
// manager in the next `PreUpdate` (the only place the manager may be changed), and answered
// once it has been applied. The reply's `data` is a one-line report, starting with `error:`
// when the request was refused; the service itself always succeeds, so the report reaches the
// client (`gz service` prints it).
//
// The wind is the world's wind entity (its `WorldLinearVelocitySeed` and `WorldLinearVelocity`
// components); the request is also published on `/world/<world>/wind` for Gazebo's
// WindEffects system, which applies the force to the links that enable wind and answers
// `/world/<world>/wind_info`. A world that does not load WindEffects keeps the components but
// nothing pushes its links.
#pragma once

#include <gz/msgs/entity.pb.h>
#include <gz/msgs/entity_factory.pb.h>
#include <gz/msgs/pose.pb.h>
#include <gz/msgs/stringmsg.pb.h>
#include <gz/msgs/wind.pb.h>
#include <gz/sim/Entity.hh>
#include <gz/sim/EntityComponentManager.hh>
#include <gz/sim/EventManager.hh>
#include <gz/sim/System.hh>
#include <gz/transport/Node.hh>
#include <sdf/Element.hh>

#include <chrono>
#include <condition_variable>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

namespace fieldproof {

/// Prefix of the services, under `/world/<world>/`.
constexpr const char* kServicePrefix = "fieldproof";
/// How long a service call waits for the simulation to step and apply its request.
constexpr std::chrono::seconds kApplyTimeout{10};

/// The service names this plugin advertises for a world (`spawn`, `remove`, ...). Inline so
/// that a client (the test) needs no link against the plugin library, which Gazebo loads.
inline std::string ServiceName(const std::string& world, const std::string& verb) {
  return "/world/" + world + "/" + kServicePrefix + "/" + verb;
}

/// A request queued by a service callback and applied in `PreUpdate`.
struct PendingRequest {
  std::function<std::string(gz::sim::EntityComponentManager&)> apply;
  std::string result;
  bool done{false};
};

class WorldEvents : public gz::sim::System,
                    public gz::sim::ISystemConfigure,
                    public gz::sim::ISystemPreUpdate {
 public:
  void Configure(const gz::sim::Entity& entity, const std::shared_ptr<const sdf::Element>& sdf,
                 gz::sim::EntityComponentManager& ecm, gz::sim::EventManager& event_mgr) override;

  void PreUpdate(const gz::sim::UpdateInfo& info, gz::sim::EntityComponentManager& ecm) override;

 private:
  bool OnSpawn(const gz::msgs::EntityFactory& request, gz::msgs::StringMsg& reply);
  bool OnRemove(const gz::msgs::Entity& request, gz::msgs::StringMsg& reply);
  bool OnTeleport(const gz::msgs::Pose& request, gz::msgs::StringMsg& reply);
  bool OnSetWind(const gz::msgs::Wind& request, gz::msgs::StringMsg& reply);

  /// Queue `apply`, wait for the next step to run it, and put its report in `reply`.
  bool Submit(std::function<std::string(gz::sim::EntityComponentManager&)> apply,
              gz::msgs::StringMsg& reply);

  std::string Spawn(const gz::msgs::EntityFactory& request, gz::sim::EntityComponentManager& ecm);
  std::string Remove(const gz::msgs::Entity& request, gz::sim::EntityComponentManager& ecm);
  std::string Teleport(const gz::msgs::Pose& request, gz::sim::EntityComponentManager& ecm);
  std::string SetWind(const gz::msgs::Wind& request, gz::sim::EntityComponentManager& ecm);

  /// The model called `name` among the world's children, or `gz::sim::kNullEntity`.
  [[nodiscard]] gz::sim::Entity ModelByName(const std::string& name,
                                            const gz::sim::EntityComponentManager& ecm) const;

  gz::sim::Entity world_entity_{gz::sim::kNullEntity};
  gz::sim::EventManager* event_manager_{nullptr};
  std::string world_name_;
  gz::transport::Node node_;
  gz::transport::Node::Publisher wind_publisher_;
  std::mutex mutex_;
  std::condition_variable applied_;
  std::vector<std::shared_ptr<PendingRequest>> pending_;
};

}  // namespace fieldproof
